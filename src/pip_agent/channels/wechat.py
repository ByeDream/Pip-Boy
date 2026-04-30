"""WeChat iLink-Bot channel (multi-account).

One ``WeChatChannel`` instance wraps *N* scanned iLink bot accounts.
Each account has its own bot token, its own ``get_updates_buf`` cursor,
its own per-peer ``context_token`` map, and its own credential file on
disk. All accounts share the one outbound ``httpx.Client`` pool and the
one send-lock.

Why one channel, many accounts
------------------------------
``ChannelManager`` keys channels by ``channel.name`` — we can't register
two ``"wechat"`` channels. Equally important, the host's reply logic
routes by :attr:`InboundMessage.channel` + ``account_id`` (tier-3), so
keeping a single ``Channel`` façade and fan-out by ``account_id``
internally maps 1:1 onto the binding table without any new abstraction.

Files on disk
-------------
Credentials live under ``<workspace>/.pip/credentials/wechat/<account_id>.json``.
Each file is a JSON object with ``token`` / ``baseUrl`` / ``accountId``
/ ``userId`` / ``get_updates_buf`` / ``savedAt``. The legacy
``<workspace>/.pip/wechat_session.json`` is **not** read here — the
host ``run_host`` does a one-time sweep at startup to delete that file
so operators aren't surprised by a ghost session after upgrading.

QR login
--------
:meth:`WeChatChannel.login` is **cancellable**. It takes a ``stop`` event
and a ``cancel`` event and polls the QR status with a short (10 s)
HTTP timeout + ``stop.wait(1.0)`` between iterations, so ``/exit`` or
``/wechat cancel`` aborts within 1-10 seconds instead of being wedged
behind a 40 s long-poll.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pip_agent.channels.base import (
    Attachment,
    Channel,
    InboundMessage,
    _detect_image_mime,
)

log = logging.getLogger(__name__)


def _wechat_operator_print(message: str) -> None:
    """Route WeChat operator text to stdout (line mode) or the TUI agent pane.

    ``login()`` historically used ``print`` + ``QRCode.print_ascii``,
    which writes behind a running Textual canvas — the QR never
    appears. :func:`pip_agent.host_io.emit_operator_plain` fans out
    through the same sink as slash-command markdown when the pump is
    attached.
    """
    from pip_agent.host_io import emit_operator_plain

    emit_operator_plain(message)


def _parse_ilink_aes_key(raw: str) -> bytes:
    """Decode an iLink AES key (3 possible formats) into 16 raw bytes."""
    import binascii
    if len(raw) == 32:
        try:
            return binascii.unhexlify(raw)
        except ValueError:
            pass
    decoded = base64.b64decode(raw)
    if len(decoded) == 16:
        return decoded
    # base64(hex-string) → hex string → bytes
    try:
        return binascii.unhexlify(decoded)
    except (ValueError, binascii.Error):
        return decoded[:16]


def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt with PKCS7 padding."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len]) * pad_len
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB decrypt with tolerant PKCS7 unpadding.

    Follows hermes-agent strategy: return raw plaintext if padding is
    malformed rather than raising, since some CDN blobs may lack proper
    padding.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _random_wechat_uin() -> str:
    """Generate X-WECHAT-UIN: random uint32 → decimal string → base64."""
    val = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(val).encode()).decode()


# ---------------------------------------------------------------------------
# _WeChatAccount — per-account state container
# ---------------------------------------------------------------------------


@dataclass
class _WeChatAccount:
    """One scanned iLink bot identity.

    Isolated from :class:`WeChatChannel` so multiple accounts can co-exist
    inside a single channel instance. Every field that used to be a
    ``self._*`` attribute on the old singleton channel lives here now.
    """

    account_id: str
    bot_token: str
    base_url: str
    user_id: str = ""
    get_updates_buf: str = ""
    cred_path: Path | None = None
    context_tokens: dict[str, str] = field(default_factory=dict)

    @property
    def is_logged_in(self) -> bool:
        return bool(self.bot_token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.bot_token,
            "baseUrl": self.base_url,
            "accountId": self.account_id,
            "userId": self.user_id,
            "get_updates_buf": self.get_updates_buf,
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @classmethod
    def from_file(cls, path: Path) -> _WeChatAccount | None:
        """Load one account from its JSON file.

        Returns ``None`` if the file is unparseable or missing the
        ``accountId`` field — corrupt files are logged and skipped, not
        raised, so a single bad file doesn't prevent other accounts from
        loading.
        """
        try:
            data = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("wechat: failed to load %s: %s", path, exc)
            return None
        account_id = str(data.get("accountId") or "")
        if not account_id:
            log.warning("wechat: credential %s missing accountId, skipped", path)
            return None
        return cls(
            account_id=account_id,
            bot_token=str(data.get("token") or ""),
            base_url=str(data.get("baseUrl") or WeChatChannel.ILINK_BASE),
            user_id=str(data.get("userId") or ""),
            get_updates_buf=str(data.get("get_updates_buf") or ""),
            cred_path=path,
        )

    def save(self) -> None:
        """Persist this account's state to its credential file."""
        if self.cred_path is None:
            return
        self.cred_path.parent.mkdir(parents=True, exist_ok=True)
        self.cred_path.write_text(
            json.dumps(self.to_dict(), indent=2), "utf-8",
        )


# ---------------------------------------------------------------------------
# WeChatChannel
# ---------------------------------------------------------------------------


class WeChatChannel(Channel):
    """WeChat iLink Bot protocol — multi-account QR login + getupdates.

    One instance holds N accounts. ``poll`` / ``send`` / ``send_typing``
    all take an ``account_id`` to select which bot identity to operate
    on; ``InboundMessage.account_id`` is populated from the same key so
    the host can route a reply back through the same bot.
    """

    name = "wechat"
    ILINK_BASE = "https://ilinkai.weixin.qq.com"
    ILINK_CDN = "https://novac2c.cdn.weixin.qq.com/c2c"

    def __init__(self, state_dir: Path) -> None:
        import httpx

        self._httpx = httpx
        self._state_dir = state_dir
        self._cred_dir = state_dir / "credentials" / "wechat"
        self._cred_dir.mkdir(parents=True, exist_ok=True)

        self._http = httpx.Client(timeout=40.0)
        self._closing = False

        self._accounts: dict[str, _WeChatAccount] = {}
        self._accounts_lock = threading.Lock()
        self._load_all_accounts()

    # -- account registry --

    def _load_all_accounts(self) -> None:
        """Populate ``self._accounts`` from ``credentials/wechat/*.json``."""
        for path in sorted(self._cred_dir.glob("*.json")):
            acc = _WeChatAccount.from_file(path)
            if acc is None:
                continue
            self._accounts[acc.account_id] = acc
            log.info(
                "wechat: loaded account %s (logged_in=%s)",
                acc.account_id, acc.is_logged_in,
            )

    def account_ids(self) -> list[str]:
        """Snapshot of currently-registered account ids."""
        with self._accounts_lock:
            return sorted(self._accounts.keys())

    def get_account(self, account_id: str) -> _WeChatAccount | None:
        with self._accounts_lock:
            return self._accounts.get(account_id)

    def add_account(self, acc: _WeChatAccount) -> None:
        """Register a freshly-scanned account + persist its credential file.

        ``acc.cred_path`` is assigned if the caller didn't set one, so
        the standard on-disk layout is used by default.
        """
        if acc.cred_path is None:
            acc.cred_path = self._cred_dir / f"{acc.account_id}.json"
        acc.save()
        with self._accounts_lock:
            self._accounts[acc.account_id] = acc
        log.info("wechat: added account %s", acc.account_id)

    def remove_account(self, account_id: str) -> bool:
        """Unregister an account and delete its credential file.

        Returns ``True`` if an account was removed. Callers that also
        want to stop a running poll thread should signal that thread's
        ``stop_event`` **before** calling this, otherwise the thread
        will see ``get_account`` return ``None`` and log spurious
        warnings until it notices stop.
        """
        with self._accounts_lock:
            acc = self._accounts.pop(account_id, None)
        if acc is None:
            return False
        if acc.cred_path is not None and acc.cred_path.exists():
            try:
                acc.cred_path.unlink()
            except OSError as exc:
                log.warning(
                    "wechat: failed to delete %s: %s", acc.cred_path, exc,
                )
        log.info("wechat: removed account %s", account_id)
        return True

    @property
    def has_any_logged_in(self) -> bool:
        with self._accounts_lock:
            return any(a.is_logged_in for a in self._accounts.values())

    # -- common headers --

    def _headers(self, acc: _WeChatAccount) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {acc.bot_token}",
            "X-WECHAT-UIN": _random_wechat_uin(),
        }

    # -- QR login flow --

    def login(
        self,
        stop: threading.Event,
        cancel: threading.Event,
        *,
        deadline_sec: float = 300.0,
    ) -> _WeChatAccount | None:
        """Interactive QR-code login — cancellable.

        Returns a fresh :class:`_WeChatAccount` on success, or ``None``
        on timeout / QR expiry / transport error / cancel-by-event.
        The caller is responsible for actually registering the returned
        account via :meth:`add_account` — this method is a pure factory
        and doesn't mutate channel state on success.

        Cancellation contract
        ---------------------
        - ``stop`` set → global shutdown (``/exit``). Return ``None``.
        - ``cancel`` set → user aborted just this login (``/wechat cancel``).
          Return ``None``.

        Both events are polled between short (~10 s) HTTP timeouts so the
        operator sees a responsive abort instead of being wedged behind
        a 40 s long-poll.
        """
        _wechat_operator_print("  [wechat] Requesting QR code...")
        try:
            resp = self._http.get(
                f"{self.ILINK_BASE}/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"},
                headers={"iLink-App-ClientVersion": "1"},
                timeout=10.0,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            _wechat_operator_print(f"  [wechat] QR request failed: {exc}")
            return None

        qrcode_id = data.get("qrcode", "")
        qrcode_url = data.get("qrcode_img_content", "")
        if not qrcode_id:
            _wechat_operator_print(f"  [wechat] Unexpected QR response: {data}")
            return None

        import qrcode as _qr

        qr = _qr.QRCode(
            error_correction=_qr.constants.ERROR_CORRECT_L,
            box_size=1,
            border=1,
        )
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        ascii_buf = io.StringIO()
        qr.print_ascii(out=ascii_buf, invert=True)
        ascii_qr = ascii_buf.getvalue().rstrip("\n")

        from pip_agent.host_io import emit_agent_markdown, is_tui_active

        qr_block = (
            "### WeChat — scan this QR\n\n"
            "  [wechat] Scan QR code with WeChat:\n\n"
            f"Fallback URL (if the ASCII QR is unreadable):\n`{qrcode_url}`\n\n"
            "```text\n"
            f"{ascii_qr}\n"
            "```\n\n"
            "  [wechat] Waiting for scan "
            "(type `/wechat cancel` to abort, `/exit` to quit)...\n"
        )
        if is_tui_active():
            emit_agent_markdown(qr_block)
        else:
            print("  [wechat] Scan QR code with WeChat:")
            print(f"  {qrcode_url}")
            qr.print_ascii(invert=True)
            print(
                "  [wechat] Waiting for scan "
                "(type /wechat cancel to abort, /exit to quit)...",
            )

        deadline = time.time() + deadline_sec
        while time.time() < deadline:
            if stop.is_set():
                _wechat_operator_print("  [wechat] QR login aborted (host shutdown).")
                return None
            if cancel.is_set():
                _wechat_operator_print("  [wechat] QR login cancelled.")
                return None
            try:
                resp = self._http.get(
                    f"{self.ILINK_BASE}/ilink/bot/get_qrcode_status",
                    params={"qrcode": qrcode_id},
                    headers={"iLink-App-ClientVersion": "1"},
                    timeout=10.0,
                )
                status_data = resp.json()
            except self._httpx.TimeoutException:
                # Short-timeout loop: swallow the timeout and check
                # stop / cancel before the next poll.
                stop.wait(1.0)
                continue
            except Exception as exc:  # noqa: BLE001
                _wechat_operator_print(f"  [wechat] QR poll error: {exc}")
                return None

            status = status_data.get("status", "wait")
            if status == "wait":
                stop.wait(1.0)
                continue
            if status == "scaned":
                _wechat_operator_print(
                    "  [wechat] QR scanned, waiting for confirmation...",
                )
                stop.wait(1.0)
                continue
            if status == "expired":
                _wechat_operator_print("  [wechat] QR code expired.")
                return None
            if status == "confirmed":
                account_id = str(status_data.get("ilink_bot_id") or "")
                if not account_id:
                    _wechat_operator_print(
                        "  [wechat] Login response missing ilink_bot_id.",
                    )
                    return None
                acc = _WeChatAccount(
                    account_id=account_id,
                    bot_token=str(status_data.get("bot_token") or ""),
                    base_url=str(
                        status_data.get("baseurl") or self.ILINK_BASE,
                    ),
                    user_id=str(status_data.get("ilink_user_id") or ""),
                    get_updates_buf="",
                )
                _wechat_operator_print(
                    f"  [wechat] Login successful (account={account_id})",
                )
                return acc
            log.warning("wechat QR unknown status: %s", status)
            stop.wait(1.0)

        _wechat_operator_print("  [wechat] QR login timed out.")
        return None

    # -- CDN media download --

    def _download_cdn_media(
        self, media: dict, aeskey_fallback: str = "", timeout: float = 30.0,
    ) -> bytes | None:
        """Download + AES-128-ECB decrypt a CDN media blob.

        Mirrors hermes-agent: tries ``encrypt_query_param`` first, falls
        back to ``full_url``, and only decrypts when an AES key is
        available.
        """
        eqp = media.get("encrypt_query_param", "")
        full_url = media.get("full_url", "")
        if not eqp and not full_url:
            return None
        # PROFILE
        from pip_agent import _profile

        with _profile.span_sync(
            "wechat.media_download",
            channel="wechat",
            has_eqp=bool(eqp),
            timeout=timeout,
        ):
            try:
                if eqp:
                    resp = self._http.get(
                        f"{self.ILINK_CDN}/download",
                        params={"encrypted_query_param": eqp},
                        timeout=timeout,
                    )
                else:
                    resp = self._http.get(full_url, timeout=timeout)
                resp.raise_for_status()
                data = resp.content
                raw_key = aeskey_fallback or media.get("aes_key", "")
                if raw_key:
                    key = _parse_ilink_aes_key(raw_key)
                    data = _aes_ecb_decrypt(data, key)
                # PROFILE
                _profile.event(
                    "wechat.media_bytes",
                    channel="wechat",
                    bytes=len(data) if data else 0,
                    decrypted=bool(raw_key),
                )
                return data
            except Exception as exc:  # noqa: BLE001
                log.warning("wechat CDN download/decrypt failed: %s", exc)
                return None

    def _upload_media(
        self, acc: _WeChatAccount, to_user_id: str,
        data: bytes, media_type: int,
    ) -> dict[str, str] | None:
        """Upload media to WeChat CDN via the iLink 3-step pipeline.

        ``media_type``: ``1`` = image, ``3`` = file (per protocol spec).

        Returns a dict with ``encrypt_query_param`` and ``aes_key``
        (base64-encoded hex string, as required by ``sendmessage``),
        or ``None`` on failure.
        """
        aes_key_bytes = os.urandom(16)
        aes_key_hex = aes_key_bytes.hex()
        filekey = uuid.uuid4().hex

        ciphertext = _aes_ecb_encrypt(data, aes_key_bytes)
        raw_md5 = hashlib.md5(data).hexdigest()  # noqa: S324

        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": len(data),
            "rawfilemd5": raw_md5,
            "filesize": len(ciphertext),
            "no_need_thumb": True,
            "aeskey": aes_key_hex,
            "base_info": {"channel_version": "1.0.0"},
        }
        try:
            resp = self._http.post(
                f"{acc.base_url}/ilink/bot/getuploadurl",
                headers=self._headers(acc),
                json=body,
                timeout=15.0,
            )
            resp.raise_for_status()
            upload_param = resp.json().get("upload_param", "")
            if not upload_param:
                log.warning("wechat getuploadurl returned empty upload_param")
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("wechat getuploadurl failed: %s", exc)
            return None

        try:
            cdn_resp = self._http.post(
                f"{self.ILINK_CDN}/upload",
                params={
                    "encrypted_query_param": upload_param,
                    "filekey": filekey,
                },
                headers={"Content-Type": "application/octet-stream"},
                content=ciphertext,
                timeout=60.0,
            )
            cdn_resp.raise_for_status()
            eqp = cdn_resp.headers.get("x-encrypted-param", "")
            if not eqp:
                log.warning("wechat CDN upload missing x-encrypted-param header")
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("wechat CDN upload failed: %s", exc)
            return None

        return {
            "encrypt_query_param": eqp,
            "aes_key": base64.b64encode(aes_key_hex.encode()).decode(),
            "filesize": len(ciphertext),
        }

    def _collect_ilink_item(
        self, item: dict, texts: list[str], atts: list[Attachment],
    ) -> None:
        """Extract text / image / voice / file from a single iLink item."""
        itype = item.get("type")
        if itype == 1:  # TEXT
            t = (item.get("text_item") or {}).get("text", "")
            if t:
                texts.append(t)
        elif itype == 2:  # IMAGE
            img = item.get("image_item") or {}
            media = img.get("media") or {}
            img_data = self._download_cdn_media(media, img.get("aeskey", ""))
            atts.append(Attachment(
                type="image", data=img_data,
                mime_type=_detect_image_mime(img_data) if img_data else "",
                text="" if img_data else "[Image]",
            ))
        elif itype == 3:  # VOICE
            voice = item.get("voice_item") or {}
            asr = voice.get("text", "")
            atts.append(Attachment(
                type="voice",
                text=asr if asr else "[Voice message]",
            ))
        elif itype == 4:  # FILE
            fi = item.get("file_item") or {}
            media = fi.get("media") or {}
            file_data = self._download_cdn_media(media, timeout=60.0)
            fname = fi.get("file_name", "file")
            text_content = ""
            if file_data:
                try:
                    text_content = file_data.decode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    pass
            atts.append(Attachment(
                type="file", data=file_data, filename=fname, text=text_content,
            ))

    # -- getupdates long-poll --

    def poll(self, account_id: str) -> list[InboundMessage]:
        """One round of getupdates for ``account_id``.

        Returns parsed messages. Empty list on transport error,
        unknown account, or no new traffic.
        """
        # PROFILE
        from pip_agent import _profile

        with _profile.span_sync("wechat.poll", channel="wechat"):
            return self._poll_inner(account_id)

    def _poll_inner(self, account_id: str) -> list[InboundMessage]:
        from pip_agent import _profile

        acc = self.get_account(account_id)
        if acc is None or not acc.is_logged_in:
            return []

        try:
            with _profile.span_sync("wechat.poll_http", channel="wechat"):
                resp = self._http.post(
                    f"{acc.base_url}/ilink/bot/getupdates",
                    headers=self._headers(acc),
                    json={
                        "get_updates_buf": acc.get_updates_buf,
                        "base_info": {"channel_version": "1.0.0"},
                    },
                    timeout=40.0,
                )
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            if not self._closing:
                log.warning(
                    "wechat getupdates error (account=%s): %s",
                    account_id, exc,
                )
            return []

        ret = data.get("ret", 0)
        if ret == -14:
            _wechat_operator_print(
                f"  [wechat] Session expired for {account_id} (-14), "
                "need re-login.",
            )
            # Blank the token so the poll loop stops hammering and
            # wait for operator action. We do NOT auto-delete the file
            # — keep the account record around so /wechat list shows it
            # as logged-out and /wechat add <agent> can re-scan.
            acc.bot_token = ""
            acc.get_updates_buf = ""
            acc.save()
            return []
        if ret != 0:
            log.warning(
                "wechat getupdates (account=%s) ret=%s: %s",
                account_id, ret, data.get("errmsg", ""),
            )
            return []

        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            acc.get_updates_buf = new_buf
            acc.save()

        results: list[InboundMessage] = []
        for msg in data.get("msgs", []):
            try:
                log.info(
                    "wechat raw inbound msg (account=%s): %s",
                    account_id,
                    json.dumps(msg, ensure_ascii=False, default=str),
                )
            except Exception:  # noqa: BLE001
                log.info("wechat raw inbound msg (account=%s) repr=%r", account_id, msg)
            if msg.get("message_type") != 1:
                continue
            if msg.get("message_state") not in (0, 2):
                continue

            from_user = msg.get("from_user_id", "")
            ctx_token = msg.get("context_token", "")
            if ctx_token and from_user:
                acc.context_tokens[from_user] = ctx_token

            # PROFILE
            with _profile.span_sync("wechat.parse_item", channel="wechat"):
                texts: list[str] = []
                atts: list[Attachment] = []
                for item in msg.get("item_list", []):
                    self._collect_ilink_item(item, texts, atts)
                    ref_item = (item.get("ref_msg") or {}).get("message_item")
                    if isinstance(ref_item, dict):
                        ref_text = (ref_item.get("text_item") or {}).get("text", "")
                        if ref_text:
                            texts.insert(0, f"[quote]\n{ref_text}\n[/quote]")
                        else:
                            self._collect_ilink_item(ref_item, [], atts)

                text = "\n".join(texts)
                if not text and not atts:
                    continue

                # PROFILE
                _profile.event(
                    "wechat.inbound_received",
                    channel="wechat",
                    text_len=len(text),
                    atts=len(atts),
                    sender=from_user,
                    account=account_id,
                )
                results.append(InboundMessage(
                    text=text,
                    sender_id=from_user,
                    channel="wechat",
                    peer_id=from_user,
                    account_id=account_id,
                    raw=msg,
                    attachments=atts,
                ))

        return results

    # -- send --

    def has_context_token(self, peer_id: str, *, account_id: str = "") -> bool:
        acc = self.get_account(account_id) if account_id else None
        if acc is None:
            return False
        return bool(acc.context_tokens.get(peer_id))

    def send(
        self, to: str, text: str, *,
        account_id: str = "", **kw: Any,
    ) -> bool:
        from pip_agent import _profile  # PROFILE
        from pip_agent.fileutil import chunk_message

        acc: _WeChatAccount | None
        if account_id:
            # Caller specified a concrete identity: require it to exist.
            # Silent fallback to "the only account" would route messages
            # out of the wrong bot on multi-account hosts the moment a
            # caller passed a stale id, so we refuse instead.
            acc = self.get_account(account_id)
        else:
            # Single-account convenience path: if only one account is
            # registered and the caller didn't specify one, fall back to
            # it. This keeps simple setups (exactly one bot) working with
            # callers that predate the multi-account refactor. Multi-
            # account hosts must always pass ``account_id``.
            with self._accounts_lock:
                acc = (
                    next(iter(self._accounts.values()))
                    if len(self._accounts) == 1 else None
                )
        if acc is None:
            _wechat_operator_print(
                f"  [wechat] Cannot send to {to}: unknown account_id "
                f"{account_id!r}",
            )
            return False

        ctx_token = acc.context_tokens.get(to, "")
        if not ctx_token:
            _wechat_operator_print(
                f"  [wechat] Cannot reply to {to} on account {acc.account_id}: "
                "no context_token",
            )
            return False

        with _profile.span_sync(
            "wechat.send", channel="wechat", text_len=len(text),
        ):
            ok = True
            for idx, chunk in enumerate(chunk_message(text, "wechat")):
                client_id = f"pip:{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"
                body = {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": to,
                        "client_id": client_id,
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": ctx_token,
                        "item_list": [{"type": 1, "text_item": {"text": chunk}}],
                    },
                    "base_info": {"channel_version": "1.0.0"},
                }
                try:
                    with _profile.span_sync(  # PROFILE
                        "wechat.send_chunk",
                        channel="wechat",
                        idx=idx,
                        bytes=len(chunk.encode("utf-8")),
                    ):
                        resp = self._http.post(
                            f"{acc.base_url}/ilink/bot/sendmessage",
                            headers=self._headers(acc),
                            json=body,
                        )
                    if resp.status_code != 200:
                        ok = False
                except Exception as exc:  # noqa: BLE001
                    log.warning("wechat sendmessage error: %s", exc)
                    ok = False
            return ok

    def _resolve_account_and_token(
        self, to: str, account_id: str,
    ) -> tuple[_WeChatAccount, str] | None:
        """Shared account / context-token resolution for media sends."""
        acc: _WeChatAccount | None
        if account_id:
            acc = self.get_account(account_id)
        else:
            with self._accounts_lock:
                acc = (
                    next(iter(self._accounts.values()))
                    if len(self._accounts) == 1 else None
                )
        if acc is None:
            _wechat_operator_print(
                f"  [wechat] Cannot send to {to}: unknown account_id "
                f"{account_id!r}",
            )
            return None
        ctx_token = acc.context_tokens.get(to, "")
        if not ctx_token:
            _wechat_operator_print(
                f"  [wechat] Cannot reply to {to} on account {acc.account_id}: "
                "no context_token",
            )
            return None
        return acc, ctx_token

    def send_image(
        self, to: str, image_data: bytes, caption: str = "",
        *, account_id: str = "", **kw: Any,
    ) -> bool:
        log.info(
            "wechat send_image called: to=%s account_id=%s bytes=%d caption_len=%d",
            to, account_id, len(image_data), len(caption),
        )
        resolved = self._resolve_account_and_token(to, account_id)
        if resolved is None:
            log.warning(
                "wechat send_image: _resolve_account_and_token returned None "
                "(to=%s account_id=%s)", to, account_id,
            )
            return False
        acc, ctx_token = resolved

        result = self._upload_media(acc, to, image_data, media_type=1)
        if result is None:
            log.warning("wechat send_image: _upload_media returned None")
            return False

        client_id = f"pip:{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": ctx_token,
                "item_list": [{
                    "type": 2,
                    "image_item": {
                        "media": {
                            "encrypt_query_param": result["encrypt_query_param"],
                            "aes_key": result["aes_key"],
                            "encrypt_type": 1,
                        },
                        "mid_size": result["filesize"],
                    },
                }],
            },
            "base_info": {"channel_version": "1.0.0"},
        }
        try:
            resp = self._http.post(
                f"{acc.base_url}/ilink/bot/sendmessage",
                headers=self._headers(acc),
                json=body,
            )
            if resp.status_code != 200:
                log.warning("wechat send_image sendmessage status=%s", resp.status_code)
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("wechat send_image error: %s", exc)
            return False

        if caption:
            self.send(to, caption, account_id=account_id)
        return True

    def send_file(
        self, to: str, file_data: bytes, filename: str = "",
        caption: str = "", *, account_id: str = "", **kw: Any,
    ) -> bool:
        log.info(
            "wechat send_file called: to=%s account_id=%s filename=%s bytes=%d caption_len=%d",
            to, account_id, filename, len(file_data), len(caption),
        )
        resolved = self._resolve_account_and_token(to, account_id)
        if resolved is None:
            log.warning(
                "wechat send_file: _resolve_account_and_token returned None "
                "(to=%s account_id=%s)", to, account_id,
            )
            return False
        acc, ctx_token = resolved

        result = self._upload_media(acc, to, file_data, media_type=3)
        if result is None:
            log.warning("wechat send_file: _upload_media returned None")
            return False

        raw_md5 = hashlib.md5(file_data).hexdigest()  # noqa: S324
        client_id = f"pip:{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": ctx_token,
                "item_list": [{
                    "type": 4,
                    "file_item": {
                        "media": {
                            "encrypt_query_param": result["encrypt_query_param"],
                            "aes_key": result["aes_key"],
                            "encrypt_type": 1,
                        },
                        "file_name": filename or "file",
                        "md5": raw_md5,
                        "len": str(len(file_data)),
                    },
                }],
            },
            "base_info": {"channel_version": "1.0.0"},
        }
        try:
            resp = self._http.post(
                f"{acc.base_url}/ilink/bot/sendmessage",
                headers=self._headers(acc),
                json=body,
            )
            if resp.status_code != 200:
                log.warning("wechat send_file sendmessage status=%s", resp.status_code)
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("wechat send_file error: %s", exc)
            return False

        if caption:
            self.send(to, caption, account_id=account_id)
        return True

    def send_typing(self, to: str, *, account_id: str = "") -> None:
        """Send typing indicator via sendtyping API (fire-and-forget)."""
        acc = self.get_account(account_id) if account_id else None
        if acc is None:
            with self._accounts_lock:
                if len(self._accounts) == 1:
                    acc = next(iter(self._accounts.values()))
        if acc is None:
            return
        ctx_token = acc.context_tokens.get(to, "")
        if not ctx_token:
            return
        try:
            self._http.post(
                f"{acc.base_url}/ilink/bot/sendtyping",
                headers=self._headers(acc),
                json={
                    "to_user_id": to,
                    "context_token": ctx_token,
                    "base_info": {"channel_version": "1.0.0"},
                },
                timeout=5.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("wechat: send_typing failed: %s", exc)

    def close(self) -> None:
        self._closing = True
        self._http.close()


# ---------------------------------------------------------------------------
# Background poll loop — one thread per account
# ---------------------------------------------------------------------------

def wechat_poll_loop(
    wechat: WeChatChannel,
    account_id: str,
    queue: list[InboundMessage],
    lock: threading.Lock,
    stop: threading.Event,
    pause: threading.Event | None = None,
) -> None:
    """Long-poll loop for one WeChat account, runs in a daemon thread.

    Cadence:

    * ``getupdates`` returned ≥ 1 message → loop immediately (interactive
      latency matters more than thrift when a conversation is active).
    * ``getupdates`` returned 0 messages → ``stop.wait(
      settings.wechat_poll_idle_sec)`` before the next call. Without
      this the loop hammers the iLink server at ~20 req/sec whenever
      the user's chat is idle. Use ``stop.wait`` rather than
      ``time.sleep`` so ``/exit`` interrupts promptly.
    * Transport error → exponential backoff, independent of the idle
      interval. Errors already drive a longer wait (``2s *
      consecutive_errors`` capped at 30 s) and should not be shortened
      by the idle setting.
    * Account unknown (e.g. removed via ``/wechat remove`` while the
      loop was running) → exit cleanly.
    """
    from pip_agent import _profile  # PROFILE
    from pip_agent.config import settings

    _wechat_operator_print(f"  [wechat] Polling started for account {account_id}")
    consecutive_errors = 0
    idle_polls_streak = 0
    while not stop.is_set():
        if pause is not None and pause.is_set():
            stop.wait(0.5)
            continue
        acc = wechat.get_account(account_id)
        if acc is None:
            log.info(
                "wechat poll loop for %s exiting: account removed",
                account_id,
            )
            return
        if not acc.is_logged_in:
            stop.wait(5.0)
            continue
        try:
            msgs = wechat.poll(account_id)
            consecutive_errors = 0
            if msgs:
                with lock:
                    queue.extend(msgs)
                if idle_polls_streak:
                    _profile.event(
                        "wechat.poll_idle_streak_end",
                        channel="wechat",
                        streak=idle_polls_streak,
                        account=account_id,
                    )
                    idle_polls_streak = 0
                continue
            idle_polls_streak += 1
            idle_wait = settings.wechat_poll_idle_sec
            if idle_wait > 0:
                if idle_polls_streak == 1:
                    _profile.event(
                        "wechat.poll_idle_backoff",
                        channel="wechat",
                        wait_sec=idle_wait,
                        account=account_id,
                    )
                stop.wait(idle_wait)
        except OSError:
            if stop.is_set():
                break
            consecutive_errors += 1
            wait = min(30.0, 2.0 * consecutive_errors)
            log.warning(
                "wechat poll OSError account=%s (retry in %.0fs)",
                account_id, wait,
            )
            stop.wait(wait)
        except Exception as exc:  # noqa: BLE001
            if stop.is_set():
                break
            consecutive_errors += 1
            wait = min(30.0, 2.0 * consecutive_errors)
            log.warning(
                "wechat poll error account=%s: %s (retry in %.0fs)",
                account_id, exc, wait,
            )
            stop.wait(wait)
