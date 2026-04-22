"""
Channel abstraction layer for multi-platform messaging.

Provides a unified InboundMessage type and Channel ABC so the agent loop
can receive/send messages through CLI, WeChat (iLink Bot protocol), or
WeCom (企业微信智能机器人 WebSocket SDK) without platform-specific logic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _detect_image_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes. Returns '' for non-images."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


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


# ---------------------------------------------------------------------------
# InboundMessage
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    """Media attachment carried alongside an InboundMessage."""

    type: str                       # "image", "file", "voice"
    data: bytes | None = None
    filename: str = ""
    mime_type: str = ""
    text: str = ""                  # ASR transcription for voice, or text file content
    saved_path: str = ""            # workdir-relative path once materialized to disk


@dataclass
class InboundMessage:
    """Platform-agnostic inbound message.  The agent loop only sees this."""

    text: str
    sender_id: str
    channel: str = ""           # "cli", "wechat", "wecom"
    peer_id: str = ""           # conversation scope key (user id for DMs)
    guild_id: str = ""          # group/guild id (e.g. WeCom chatid)
    account_id: str = ""        # bot account id (for T3 routing)
    is_group: bool = False
    agent_id: str = ""          # routing override: skip binding_table.resolve
    raw: dict = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)
    # Host-scheduler coalescing key. Empty for user/channel messages. For
    # scheduler-injected payloads it identifies the "logical job" so
    # :class:`HostScheduler` can refuse to stack a second copy on the queue
    # while the first is still pending or in flight. ``AgentHost`` must call
    # ``scheduler.ack(source_job_id)`` after processing so subsequent ticks
    # can re-enqueue. See ``host_scheduler._pending_key_*`` for the format.
    source_job_id: str = ""


# ---------------------------------------------------------------------------
# Channel ABC
# ---------------------------------------------------------------------------

class Channel(ABC):
    name: str = "unknown"

    @property
    def send_lock(self) -> threading.Lock:
        """Per-instance lock that serializes outbound writes.

        Multiple lanes may call ``send`` concurrently once agent turns run
        in parallel. Chunked messages must not interleave on the wire, and
        underlying async / HTTP clients are not universally thread-safe,
        so every network-facing send should hold this lock.
        """
        lk = getattr(self, "_send_lock", None)
        if lk is None:
            lk = threading.Lock()
            # object.__setattr__ so dataclass-like subclasses still work.
            object.__setattr__(self, "_send_lock", lk)
        return lk

    @abstractmethod
    def send(self, to: str, text: str, **kw: Any) -> bool:
        ...

    def send_image(self, to: str, image_data: bytes, caption: str = "", **kw: Any) -> bool:
        """Send an image. Subclasses override; default is no-op."""
        return False

    def send_file(
        self, to: str, file_data: bytes, filename: str = "",
        caption: str = "", **kw: Any,
    ) -> bool:
        """Send a file. Subclasses override; default is no-op."""
        return False

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# send_with_retry — retry + backoff wrapper for Channel.send
# ---------------------------------------------------------------------------

BACKOFF_SCHEDULE = [2.0, 5.0, 15.0]  # seconds per retry attempt


def send_with_retry(ch: Channel, to: str, text: str) -> bool:
    """Chunk *text* per channel limits, then send each chunk with retries.

    Returns True only if every chunk was delivered.  CLI channels skip
    chunking and retry (stdout never fails). Holds ``ch.send_lock`` so that
    concurrent callers (one per lane) never interleave chunks or trigger
    races inside the underlying HTTP / WebSocket client.
    """
    if ch.name == "cli":
        with ch.send_lock:
            return ch.send(to, text)

    from pip_agent.fileutil import chunk_message

    chunks = chunk_message(text, ch.name)
    all_ok = True
    with ch.send_lock:
        for chunk in chunks:
            ok = ch.send(to, chunk)
            if ok:
                continue
            for delay in BACKOFF_SCHEDULE:
                jitter = delay * 0.2 * (random.random() - 0.5)
                time.sleep(delay + jitter)
                ok = ch.send(to, chunk)
                if ok:
                    break
            if not ok:
                all_ok = False
                log.warning(
                    "send_with_retry: gave up after %d retries to %s on %s",
                    len(BACKOFF_SCHEDULE), to, ch.name,
                )
    return all_ok


# ---------------------------------------------------------------------------
# CLIChannel
# ---------------------------------------------------------------------------

class CLIChannel(Channel):
    name = "cli"

    def send(self, to: str, text: str, **kw: Any) -> bool:
        print()
        print("================================================")
        print(text)
        return True


# ---------------------------------------------------------------------------
# WeChatChannel — iLink Bot long-poll protocol
# ---------------------------------------------------------------------------

def _random_wechat_uin() -> str:
    """Generate X-WECHAT-UIN: random uint32 → decimal string → base64."""
    val = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(val).encode()).decode()


class WeChatChannel(Channel):
    """WeChat iLink Bot protocol — QR login + getupdates long-poll."""

    name = "wechat"
    ILINK_BASE = "https://ilinkai.weixin.qq.com"
    ILINK_CDN = "https://novac2c.cdn.weixin.qq.com/c2c"

    def __init__(self, state_dir: Path) -> None:
        import httpx

        self._httpx = httpx
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._cred_path = state_dir / "wechat_session.json"

        self._http = httpx.Client(timeout=40.0)
        self._bot_token: str = ""
        self._base_url: str = self.ILINK_BASE
        self._account_id: str = ""
        self._user_id: str = ""
        self._get_updates_buf: str = ""
        self._closing = False

        self._context_tokens: dict[str, str] = {}

        self._load_creds()

    # -- credential persistence --

    def _load_creds(self) -> None:
        if not self._cred_path.exists():
            return
        try:
            data = json.loads(self._cred_path.read_text("utf-8"))
            self._bot_token = data.get("token", "")
            self._base_url = data.get("baseUrl", self.ILINK_BASE)
            self._account_id = data.get("accountId", "")
            self._user_id = data.get("userId", "")
            self._get_updates_buf = data.get("get_updates_buf", "")
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            log.debug("wechat: failed to load credentials: %s", exc)

    def _save_creds(self) -> None:
        data = {
            "token": self._bot_token,
            "baseUrl": self._base_url,
            "accountId": self._account_id,
            "userId": self._user_id,
            "get_updates_buf": self._get_updates_buf,
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._cred_path.write_text(json.dumps(data, indent=2), "utf-8")

    def _clear_creds(self) -> None:
        self._bot_token = ""
        self._get_updates_buf = ""
        if self._cred_path.exists():
            self._cred_path.unlink()

    # -- common headers --

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self._bot_token}",
            "X-WECHAT-UIN": _random_wechat_uin(),
        }

    # -- QR login flow --

    @property
    def is_logged_in(self) -> bool:
        return bool(self._bot_token)

    def login(self) -> bool:
        """Interactive QR-code login.  Returns True on success."""
        print("  [wechat] Requesting QR code...")
        try:
            resp = self._http.get(
                f"{self.ILINK_BASE}/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"},
                headers={"iLink-App-ClientVersion": "1"},
            )
            data = resp.json()
        except Exception as exc:
            print(f"  [wechat] QR request failed: {exc}")
            return False

        qrcode_id = data.get("qrcode", "")
        qrcode_url = data.get("qrcode_img_content", "")
        if not qrcode_id:
            print(f"  [wechat] Unexpected QR response: {data}")
            return False

        print("  [wechat] Scan QR code with WeChat:")
        print(f"  {qrcode_url}")

        import qrcode as _qr
        qr = _qr.QRCode(error_correction=_qr.constants.ERROR_CORRECT_L, box_size=1, border=1)
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)

        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                resp = self._http.get(
                    f"{self.ILINK_BASE}/ilink/bot/get_qrcode_status",
                    params={"qrcode": qrcode_id},
                    headers={"iLink-App-ClientVersion": "1"},
                    timeout=40.0,
                )
                status_data = resp.json()
            except self._httpx.TimeoutException:
                continue
            except Exception as exc:
                print(f"  [wechat] QR poll error: {exc}")
                return False

            status = status_data.get("status", "wait")
            if status == "wait":
                continue
            if status == "scaned":
                print("  [wechat] QR scanned, waiting for confirmation...")
                continue
            if status == "expired":
                print("  [wechat] QR code expired.")
                return False
            if status == "confirmed":
                self._bot_token = status_data.get("bot_token", "")
                self._base_url = status_data.get("baseurl", self.ILINK_BASE)
                self._account_id = status_data.get("ilink_bot_id", "")
                self._user_id = status_data.get("ilink_user_id", "")
                self._get_updates_buf = ""
                self._save_creds()
                print(f"  [wechat] Login successful (account={self._account_id})")
                return True
            log.warning("wechat QR unknown status: %s", status)

        print("  [wechat] QR login timed out (5 min).")
        return False

    # -- CDN media download --

    def _download_cdn_media(
        self, media: dict, aeskey_fallback: str = "", timeout: float = 30.0,
    ) -> bytes | None:
        """Download + AES-128-ECB decrypt a CDN media blob.

        Mirrors hermes-agent: tries ``encrypt_query_param`` first, falls back
        to ``full_url``, and only decrypts when an AES key is available.
        """
        eqp = media.get("encrypt_query_param", "")
        full_url = media.get("full_url", "")
        if not eqp and not full_url:
            return None
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
            return data
        except Exception as exc:
            log.warning("wechat CDN download/decrypt failed: %s", exc)
            return None

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

    def poll(self) -> list[InboundMessage]:
        """One round of getupdates.  Returns parsed messages."""
        try:
            resp = self._http.post(
                f"{self._base_url}/ilink/bot/getupdates",
                headers=self._headers(),
                json={
                    "get_updates_buf": self._get_updates_buf,
                    "base_info": {"channel_version": "1.0.0"},
                },
                timeout=40.0,
            )
            data = resp.json()
        except Exception as exc:
            if not self._closing:
                log.warning("wechat getupdates error: %s", exc)
            return []

        ret = data.get("ret", 0)
        if ret == -14:
            print("  [wechat] Session expired (-14), need re-login.")
            self._clear_creds()
            return []
        if ret != 0:
            log.warning("wechat getupdates ret=%s: %s", ret, data.get("errmsg", ""))
            return []

        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf
            self._save_creds()

        results: list[InboundMessage] = []
        for msg in data.get("msgs", []):
            if msg.get("message_type") != 1:
                continue
            if msg.get("message_state") not in (0, 2):
                continue

            from_user = msg.get("from_user_id", "")
            ctx_token = msg.get("context_token", "")
            if ctx_token and from_user:
                self._context_tokens[from_user] = ctx_token

            texts: list[str] = []
            atts: list[Attachment] = []
            for item in msg.get("item_list", []):
                self._collect_ilink_item(item, texts, atts)
                ref_item = (item.get("ref_msg") or {}).get("message_item")
                if isinstance(ref_item, dict):
                    self._collect_ilink_item(ref_item, [], atts)

            text = "\n".join(texts)
            if not text and not atts:
                continue

            results.append(InboundMessage(
                text=text,
                sender_id=from_user,
                channel="wechat",
                peer_id=from_user,
                account_id=self._account_id,
                raw=msg,
                attachments=atts,
            ))

        return results

    # -- send --

    def has_context_token(self, peer_id: str) -> bool:
        return bool(self._context_tokens.get(peer_id))

    def send(self, to: str, text: str, **kw: Any) -> bool:
        from pip_agent.fileutil import chunk_message

        ctx_token = self._context_tokens.get(to, "")
        if not ctx_token:
            print(f"  [wechat] Cannot reply to {to}: no context_token")
            return False

        ok = True
        for chunk in chunk_message(text, "wechat"):
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
                resp = self._http.post(
                    f"{self._base_url}/ilink/bot/sendmessage",
                    headers=self._headers(),
                    json=body,
                )
                if resp.status_code != 200:
                    ok = False
            except Exception as exc:
                log.warning("wechat sendmessage error: %s", exc)
                ok = False
        return ok

    def send_typing(self, to: str) -> None:
        """Send typing indicator via sendtyping API (fire-and-forget)."""
        ctx_token = self._context_tokens.get(to, "")
        if not ctx_token:
            return
        try:
            self._http.post(
                f"{self._base_url}/ilink/bot/sendtyping",
                headers=self._headers(),
                json={
                    "to_user_id": to,
                    "context_token": ctx_token,
                    "base_info": {"channel_version": "1.0.0"},
                },
                timeout=5.0,
            )
        except Exception as exc:
            log.debug("wechat: send_typing failed: %s", exc)

    def close(self) -> None:
        self._closing = True
        self._http.close()


# ---------------------------------------------------------------------------
# WecomChannel — 企业微信智能机器人 WebSocket
# ---------------------------------------------------------------------------

try:
    from aibot import WSClient, WSClientOptions, generate_req_id  # type: ignore[import-untyped]
    _HAS_WECOM_SDK = True
except ImportError:
    _HAS_WECOM_SDK = False


class WecomChannel(Channel):
    """WeCom (企业微信) smart-bot channel via official WebSocket SDK."""

    name = "wecom"

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        msg_queue: list[InboundMessage],
        q_lock: threading.Lock,
    ) -> None:
        if not _HAS_WECOM_SDK:
            raise RuntimeError(
                "WecomChannel requires wecom-aibot-python-sdk: "
                "pip install wecom-aibot-python-sdk"
            )
        self._bot_id = bot_id
        self._bot_secret = bot_secret
        self._msg_queue = msg_queue
        self._q_lock = q_lock

        self._ws_client: Any = None
        self._ws_loop: Any = None
        self._pending_frames: dict[str, Any] = {}

    _DOWNLOAD_TIMEOUT = 30  # seconds

    def start(self, stop_event: threading.Event) -> None:
        """Create WSClient, register handlers, run in current thread's event loop."""
        import asyncio

        class _QuietLogger:
            def debug(self, msg: str, *args: object) -> None:
                pass
            def info(self, msg: str, *args: object) -> None:
                log.info("[AiBotSDK] %s", msg)
            def warn(self, msg: str, *args: object) -> None:
                log.warning("[AiBotSDK] %s", msg)
            def error(self, msg: str, *args: object) -> None:
                log.error("[AiBotSDK] %s", msg)

        self._ws_client = WSClient(
            WSClientOptions(
                bot_id=self._bot_id,
                secret=self._bot_secret,
                logger=_QuietLogger(),
            )
        )
        ws = self._ws_client

        # -- helpers (closures) --

        def _parse_frame(body: dict) -> tuple[str, str, bool, str, str]:
            """Return (sender_id, peer_id, is_group, guild_id, chat_type)."""
            sender = body.get("from", {})
            if isinstance(sender, dict):
                sender_id = (
                    sender.get("userid")
                    or sender.get("user_id")
                    or sender.get("open_id")
                    or ""
                )
            elif isinstance(sender, str):
                sender_id = sender
            else:
                sender_id = ""
            chat_id = body.get("chatid", "")
            chat_type = body.get("chattype", "") or body.get("chat_type", "")
            is_group = chat_type == "group"
            guild_id = chat_id if is_group else ""
            peer_id = chat_id if chat_id else sender_id
            return sender_id, peer_id, is_group, guild_id, chat_type

        async def _download_media(
            url: str, aeskey: str | None = None,
        ) -> tuple[bytes | None, str | None]:
            # ``aibot.WSClient.download_file`` returns ``(bytes, filename)``
            # where ``filename`` is parsed from the gateway's
            # Content-Disposition. WeCom's own ``file`` body dict rarely
            # carries ``filename``/``file_name``/``name`` for user
            # uploads, so if we drop this the agent sees ``[File: file]``
            # with no extension to work from. Plumb it through.
            try:
                data, fname = await asyncio.wait_for(
                    ws.download_file(url, aeskey),
                    timeout=self._DOWNLOAD_TIMEOUT,
                )
                return data, fname
            except Exception as exc:
                log.warning("wecom media download failed: %s", exc)
                return None, None

        async def _collect_quote_attachments(body: dict) -> list[Attachment]:
            """Download media from a quoted (reply-to) message.

            Field-based detection (like the main handler) so we don't
            depend on the quote's msgtype being accurate.
            """
            quote = body.get("quote")
            if not quote:
                return []
            atts: list[Attachment] = []

            log.debug(
                "wecom quote: msgtype=%s keys=%s",
                quote.get("msgtype", ""), sorted(quote.keys()),
            )

            if quote.get("image", {}).get("url"):
                img = quote["image"]
                data, _fname = await _download_media(
                    img["url"], img.get("aeskey"),
                )
                atts.append(Attachment(
                    type="image", data=data,
                    mime_type=_detect_image_mime(data) if data else "",
                    text="" if data else "[Quoted image]",
                ))

            if quote.get("file", {}).get("url"):
                fi = quote["file"]
                data, dl_fname = await _download_media(
                    fi["url"], fi.get("aeskey"),
                )
                # Prefer the gateway-supplied filename (from the
                # download response) over the body dict — WeCom's
                # file body usually lacks ``filename``/``file_name``.
                fname = (
                    dl_fname
                    or fi.get("filename") or fi.get("file_name")
                    or fi.get("name") or "file"
                )
                if data and _detect_image_mime(data):
                    atts.append(Attachment(
                        type="image", data=data,
                        mime_type=_detect_image_mime(data),
                    ))
                else:
                    text_content = ""
                    if data:
                        try:
                            text_content = data.decode("utf-8")
                        except (UnicodeDecodeError, ValueError):
                            pass
                    atts.append(Attachment(
                        type="file", data=data, filename=fname,
                        text=text_content,
                    ))

            if quote.get("voice", {}).get("content"):
                atts.append(Attachment(
                    type="voice", text=quote["voice"]["content"],
                ))

            if not atts and quote.get("text", {}).get("content"):
                quoted_text = quote["text"]["content"]
                sender = quote.get("from", {})
                who = sender.get("userid") or sender.get("name") or ""
                label = f"quoted message from {who}" if who else "quoted message"
                atts.append(Attachment(
                    type="file", filename=label, text=quoted_text,
                ))
            return atts

        def _enqueue(frame: dict, text: str, attachments: list[Attachment]) -> None:
            body = frame.get("body", {})
            sender_id, peer_id, is_group, guild_id, chat_type = _parse_frame(body)
            if not text and not attachments:
                return
            log.debug(
                "wecom enqueue: type=%s peer=%s sender=%s atts=%d",
                body.get("msgtype", "?"), peer_id, sender_id, len(attachments),
            )
            self._pending_frames[peer_id] = frame
            msg = InboundMessage(
                text=text,
                sender_id=sender_id,
                channel="wecom",
                peer_id=peer_id,
                guild_id=guild_id,
                account_id=self._bot_id,
                is_group=is_group,
                raw=frame,
                attachments=attachments,
            )
            with self._q_lock:
                self._msg_queue.append(msg)

        # -- event handlers --

        @ws.on("authenticated")
        def _on_auth():
            print("  [wecom] Authenticated")

        @ws.on("disconnected")
        def _on_disconnect(reason: str = ""):
            print(f"  [wecom] Disconnected: {reason}")

        @ws.on("error")
        def _on_error(err: Exception):
            log.warning("wecom error: %s", err)

        @ws.on("message")
        async def _on_message(frame: dict):
            """Unified handler — detect fields regardless of msgtype.

            WeCom may deliver image/file fields under unexpected msgtypes
            (e.g. "stream", or text+image without "mixed"), so we check
            all fields unconditionally, matching pipi's strategy.
            """
            body = frame.get("body", {})
            if not body:
                return
            msgtype = body.get("msgtype", "")
            text_parts: list[str] = []
            atts: list[Attachment] = []

            log.debug("wecom msg: msgtype=%s keys=%s", msgtype, sorted(body.keys()))

            # -- mixed: iterate msg_item explicitly --
            if msgtype == "mixed" and body.get("mixed", {}).get("msg_item"):
                for item in body["mixed"]["msg_item"]:
                    mt = item.get("msgtype", "")
                    if mt == "text":
                        t = item.get("text", {}).get("content", "")
                        if t:
                            text_parts.append(t)
                    elif mt == "image":
                        img = item.get("image", {})
                        url = img.get("url", "")
                        if url:
                            data, _fname = await _download_media(
                                url, img.get("aeskey"),
                            )
                            atts.append(Attachment(
                                type="image", data=data,
                                mime_type=_detect_image_mime(data) if data else "",
                                text="" if data else "[Image]",
                            ))
                        else:
                            atts.append(Attachment(type="image", text="[Image]"))
            else:
                # -- field detection: check text, image, file, voice
                #    unconditionally (a message may carry multiple) --
                text_content = body.get("text", {}).get("content", "")
                if text_content:
                    text_parts.append(text_content)

                if body.get("image", {}).get("url"):
                    img = body["image"]
                    data, _fname = await _download_media(
                        img["url"], img.get("aeskey"),
                    )
                    atts.append(Attachment(
                        type="image", data=data,
                        mime_type=_detect_image_mime(data) if data else "",
                        text="" if data else "[Image]",
                    ))

                if body.get("file", {}).get("url"):
                    fi = body["file"]
                    data, dl_fname = await _download_media(
                        fi["url"], fi.get("aeskey"),
                    )
                    # Prefer the gateway-supplied filename over the body
                    # dict: WeCom usually omits ``filename`` on file
                    # uploads so without this the agent would see
                    # ``[File: file]`` with no extension.
                    fname = (
                        dl_fname
                        or fi.get("filename") or fi.get("file_name")
                        or fi.get("name") or "file"
                    )
                    text_content_f = ""
                    if data:
                        try:
                            text_content_f = data.decode("utf-8")
                        except (UnicodeDecodeError, ValueError):
                            pass
                    atts.append(Attachment(
                        type="file", data=data, filename=fname, text=text_content_f,
                    ))

                if body.get("voice", {}).get("content"):
                    asr = body["voice"]["content"]
                    atts.append(Attachment(type="voice", text=asr))
                elif msgtype == "voice":
                    atts.append(Attachment(
                        type="voice", text="[Voice message]",
                    ))

                # -- link messages (forwarded articles / shared links) --
                link = body.get("link") or {}
                if link:
                    parts = []
                    if link.get("title"):
                        parts.append(link["title"])
                    if link.get("desc"):
                        parts.append(link["desc"])
                    if link.get("url"):
                        parts.append(link["url"])
                    if parts:
                        text_parts.append("\n".join(parts))

                # -- chat_record / merged-forward messages --
                chat_record = body.get("chat_record") or {}
                if chat_record:
                    items = chat_record.get("item") or chat_record.get("items") or []
                    for rec in items:
                        title = rec.get("title", "")
                        content_r = rec.get("content", "")
                        if title or content_r:
                            text_parts.append(
                                f"{title}: {content_r}" if title else content_r
                            )
                    if not items:
                        desc = chat_record.get("title") or chat_record.get("desc") or ""
                        if desc:
                            text_parts.append(f"[Chat Record] {desc}")

                # -- news / miniprogram / markdown --
                news = body.get("news") or {}
                if news.get("articles"):
                    for art in news["articles"]:
                        parts = []
                        if art.get("title"):
                            parts.append(art["title"])
                        if art.get("description"):
                            parts.append(art["description"])
                        if art.get("url"):
                            parts.append(art["url"])
                        if parts:
                            text_parts.append("\n".join(parts))

                md = body.get("markdown") or {}
                if md.get("content"):
                    text_parts.append(md["content"])

            # -- quote (reply-to) attachments --
            atts.extend(await _collect_quote_attachments(body))

            text = "\n".join(text_parts)
            if not text and not atts:
                _body_keys = {
                    k: (list(v.keys()) if isinstance(v, dict) else type(v).__name__)
                    for k, v in body.items()
                }
                log.debug(
                    "wecom DROPPED msgtype=%s structure=%s",
                    msgtype,
                    json.dumps(_body_keys, ensure_ascii=False, default=str)[:2000],
                )
                return
            _enqueue(frame, text, atts)

        async def _run():
            self._ws_loop = asyncio.get_running_loop()
            await ws.connect()
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
            ws.disconnect()

        asyncio.run(_run())

    def send(self, to: str, text: str, **kw: Any) -> bool:
        frame = self._pending_frames.get(to)
        if not frame or not self._ws_client:
            log.warning("wecom send: no frame for %s, trying send_message", to)
            return self._send_proactive(to, text)
        self._run_async(self._reply_async(frame, text))
        return True

    async def _reply_async(self, frame: dict, text: str) -> None:
        stream_id = generate_req_id("stream")
        await self._ws_client.reply_stream(frame, stream_id, text, True)

    # -- media upload pipeline (WebSocket protocol, no SDK uploadMedia needed) --

    _UPLOAD_CHUNK_SIZE = 512 * 1024  # 512 KB per chunk
    _MAX_UPLOAD_CHUNKS = 100

    async def _ws_command(self, body: dict, cmd: str) -> dict:
        """Send a raw WS command and return the response frame."""
        req_id = generate_req_id(cmd)
        frame: dict = {"headers": {"req_id": req_id}}
        return await self._ws_client.reply(frame, body, cmd)

    async def _upload_media_bytes(
        self, data: bytes, media_type: str, filename: str,
    ) -> str:
        """Upload media via init→chunk→finish and return media_id."""
        total_size = len(data)
        total_chunks = (total_size + self._UPLOAD_CHUNK_SIZE - 1) // self._UPLOAD_CHUNK_SIZE
        if total_chunks > self._MAX_UPLOAD_CHUNKS:
            raise ValueError(
                f"File too large: {total_chunks} chunks exceeds {self._MAX_UPLOAD_CHUNKS}"
            )

        init_resp = await self._ws_command({
            "type": media_type,
            "filename": filename,
            "total_size": total_size,
            "total_chunks": total_chunks,
            "md5": hashlib.md5(data).hexdigest(),
        }, "aibot_upload_media_init")

        init_body = init_resp.get("body", {})
        upload_id = str(init_body.get("upload_id", "")).strip()
        if not upload_id:
            raise RuntimeError(f"upload_media_init: no upload_id in {init_resp}")

        for idx, start in enumerate(range(0, total_size, self._UPLOAD_CHUNK_SIZE)):
            chunk = data[start:start + self._UPLOAD_CHUNK_SIZE]
            await self._ws_command({
                "upload_id": upload_id,
                "chunk_index": idx,
                "base64_data": base64.b64encode(chunk).decode("ascii"),
            }, "aibot_upload_media_chunk")

        finish_resp = await self._ws_command(
            {"upload_id": upload_id}, "aibot_upload_media_finish",
        )
        finish_body = finish_resp.get("body", {})
        media_id = str(finish_body.get("media_id", "")).strip()
        if not media_id:
            raise RuntimeError(f"upload_media_finish: no media_id in {finish_resp}")
        return media_id

    async def _send_media_msg(self, chat_id: str, media_type: str, media_id: str) -> None:
        await self._ws_client.send_message(chat_id, {
            "msgtype": media_type,
            media_type: {"media_id": media_id},
        })

    def _run_async(self, coro: Any) -> Any:
        """Schedule an async coroutine on the WS thread's event loop."""
        import asyncio
        import concurrent.futures

        loop = self._ws_loop
        if loop is None or loop.is_closed():
            log.warning("wecom _run_async: WS loop not available")
            return None
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            log.warning("wecom _run_async: timed out")
            future.cancel()
            return None
        except Exception:
            log.exception("wecom _run_async: coroutine failed")
            return None

    def send_image(self, to: str, image_data: bytes, caption: str = "", **kw: Any) -> bool:
        if not self._ws_client or not image_data:
            return False
        try:
            async def _do() -> None:
                media_id = await self._upload_media_bytes(image_data, "image", "image.jpg")
                await self._send_media_msg(to, "image", media_id)
                if caption:
                    await self._ws_client.send_message(to, {
                        "msgtype": "markdown",
                        "markdown": {"content": caption},
                    })
            self._run_async(_do())
            return True
        except Exception as exc:
            log.warning("wecom send_image error: %s", exc)
            return False

    def send_file(
        self, to: str, file_data: bytes, filename: str = "",
        caption: str = "", **kw: Any,
    ) -> bool:
        if not self._ws_client or not file_data:
            return False
        try:
            async def _do() -> None:
                media_id = await self._upload_media_bytes(
                    file_data, "file", filename or "file",
                )
                await self._send_media_msg(to, "file", media_id)
                if caption:
                    await self._ws_client.send_message(to, {
                        "msgtype": "markdown",
                        "markdown": {"content": caption},
                    })
            self._run_async(_do())
            return True
        except Exception as exc:
            log.warning("wecom send_file error: %s", exc)
            return False

    def _send_proactive(self, to: str, text: str) -> bool:
        """Fallback: proactive push via send_message (markdown)."""
        from pip_agent.fileutil import chunk_message

        if not self._ws_client:
            return False
        ok = True
        for chunk in chunk_message(text, "wecom"):
            try:
                self._run_async(self._ws_client.send_message(to, {
                    "msgtype": "markdown",
                    "markdown": {"content": chunk},
                }))
            except Exception as exc:
                log.warning("wecom send_message error: %s", exc)
                ok = False
        return ok

    def close(self) -> None:
        if self._ws_client:
            try:
                self._ws_client.disconnect()
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# ChannelManager
# ---------------------------------------------------------------------------

class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel
        print(f"  [+] Channel registered: {channel.name}")

    def get(self, name: str) -> Channel | None:
        return self.channels.get(name)

    def list_channels(self) -> list[str]:
        return list(self.channels.keys())

    def close_all(self) -> None:
        for ch in self.channels.values():
            try:
                ch.close()
            except Exception as exc:
                log.warning("error closing channel %s: %s", ch.name, exc)


# ---------------------------------------------------------------------------
# Background poll loops
# ---------------------------------------------------------------------------

def wechat_poll_loop(
    wechat: WeChatChannel,
    queue: list[InboundMessage],
    lock: threading.Lock,
    stop: threading.Event,
    pause: threading.Event | None = None,
) -> None:
    """Long-poll loop for WeChat, runs in a daemon thread."""
    print("  [wechat] Polling started")
    consecutive_errors = 0
    while not stop.is_set():
        if pause is not None and pause.is_set():
            stop.wait(0.5)
            continue
        if not wechat.is_logged_in:
            stop.wait(5.0)
            continue
        try:
            msgs = wechat.poll()
            consecutive_errors = 0
            if msgs:
                with lock:
                    queue.extend(msgs)
        except OSError:
            if stop.is_set():
                break
            consecutive_errors += 1
            wait = min(30.0, 2.0 * consecutive_errors)
            log.warning("wechat poll OSError (retry in %.0fs)", wait)
            stop.wait(wait)
        except Exception as exc:
            if stop.is_set():
                break
            consecutive_errors += 1
            wait = min(30.0, 2.0 * consecutive_errors)
            log.warning("wechat poll error: %s (retry in %.0fs)", exc, wait)
            stop.wait(wait)


def wecom_ws_loop(
    wecom: WecomChannel,
    stop: threading.Event,
) -> None:
    """WebSocket loop for WeCom, runs in a daemon thread."""
    print("  [wecom] WebSocket loop starting")
    wecom.start(stop)
