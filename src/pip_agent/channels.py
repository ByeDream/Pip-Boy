"""
Channel abstraction layer for multi-platform messaging.

Provides a unified InboundMessage type and Channel ABC so the agent loop
can receive/send messages through CLI, WeChat (iLink Bot protocol), or
WeCom (企业微信智能机器人 WebSocket SDK) without platform-specific logic.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# InboundMessage
# ---------------------------------------------------------------------------

@dataclass
class InboundMessage:
    """Platform-agnostic inbound message.  The agent loop only sees this."""

    text: str
    sender_id: str
    channel: str = ""           # "cli", "wechat", "wecom"
    peer_id: str = ""           # conversation scope key
    is_group: bool = False
    raw: dict = field(default_factory=dict)


def build_session_key(channel: str, peer_id: str) -> str:
    return f"{channel}:{peer_id}"


# ---------------------------------------------------------------------------
# Channel ABC
# ---------------------------------------------------------------------------

class Channel(ABC):
    name: str = "unknown"

    @abstractmethod
    def send(self, to: str, text: str, **kw: Any) -> bool:
        ...

    def close(self) -> None:
        pass


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

try:
    import httpx as _httpx
    _HAS_HTTPX = True
except ImportError:
    _httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False


def _random_wechat_uin() -> str:
    """Generate X-WECHAT-UIN: random uint32 → decimal string → base64."""
    val = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(val).encode()).decode()


class WeChatChannel(Channel):
    """WeChat iLink Bot protocol — QR login + getupdates long-poll."""

    name = "wechat"
    ILINK_BASE = "https://ilinkai.weixin.qq.com"
    MAX_TEXT_LEN = 2000

    def __init__(self, state_dir: Path) -> None:
        if not _HAS_HTTPX:
            raise RuntimeError("WeChatChannel requires httpx: pip install httpx")
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._cred_path = state_dir / "wechat_session.json"

        self._http = _httpx.Client(timeout=40.0)
        self._bot_token: str = ""
        self._base_url: str = self.ILINK_BASE
        self._account_id: str = ""
        self._user_id: str = ""
        self._get_updates_buf: str = ""

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
        except Exception:
            pass

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

        print(f"  [wechat] Scan QR code with WeChat:")
        print(f"  {qrcode_url}")

        try:
            import qrcode as _qr
            qr = _qr.QRCode(error_correction=_qr.constants.ERROR_CORRECT_L, box_size=1, border=1)
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            pass

        while True:
            try:
                resp = self._http.get(
                    f"{self.ILINK_BASE}/ilink/bot/get_qrcode_status",
                    params={"qrcode": qrcode_id},
                    headers={"iLink-App-ClientVersion": "1"},
                    timeout=40.0,
                )
                status_data = resp.json()
            except _httpx.TimeoutException:
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
            for item in msg.get("item_list", []):
                if item.get("type") == 1:
                    t = item.get("text_item", {}).get("text", "")
                    if t:
                        texts.append(t)

            text = "\n".join(texts)
            if not text:
                continue

            results.append(InboundMessage(
                text=text,
                sender_id=from_user,
                channel="wechat",
                peer_id=from_user,
                raw=msg,
            ))

        return results

    # -- send --

    def send(self, to: str, text: str, **kw: Any) -> bool:
        ctx_token = self._context_tokens.get(to, "")
        if not ctx_token:
            log.warning("wechat send: no context_token for %s", to)
            return False

        ok = True
        for chunk in self._split_text(text):
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
        """Send typing indicator via sendtyping API."""
        ctx_token = self._context_tokens.get(to, "")
        try:
            self._http.post(
                f"{self._base_url}/ilink/bot/sendtyping",
                headers=self._headers(),
                json={
                    "to_user_id": to,
                    "context_token": ctx_token,
                    "base_info": {"channel_version": "1.0.0"},
                },
            )
        except Exception:
            pass

    def _split_text(self, text: str) -> list[str]:
        if len(text) <= self.MAX_TEXT_LEN:
            return [text]
        chunks: list[str] = []
        while text:
            if len(text) <= self.MAX_TEXT_LEN:
                chunks.append(text)
                break
            cut = text.rfind("\n\n", 0, self.MAX_TEXT_LEN)
            if cut <= 0:
                cut = text.rfind("\n", 0, self.MAX_TEXT_LEN)
            if cut <= 0:
                cut = text.rfind(" ", 0, self.MAX_TEXT_LEN)
            if cut <= 0:
                cut = self.MAX_TEXT_LEN
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    def close(self) -> None:
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
        self._pending_frames: dict[str, Any] = {}

    def start(self, stop_event: threading.Event) -> None:
        """Create WSClient, register handlers, run in current thread's event loop."""
        import asyncio

        self._ws_client = WSClient(
            WSClientOptions(
                bot_id=self._bot_id,
                secret=self._bot_secret,
            )
        )
        ws = self._ws_client

        @ws.on("authenticated")
        def _on_auth():
            print("  [wecom] Authenticated")

        @ws.on("disconnected")
        def _on_disconnect(reason: str = ""):
            print(f"  [wecom] Disconnected: {reason}")

        @ws.on("error")
        def _on_error(err: Exception):
            log.warning("wecom error: %s", err)

        @ws.on("message.text")
        async def _on_text(frame: dict):
            body = frame.get("body", {})
            sender = body.get("from", {})
            sender_id = ""
            if isinstance(sender, dict):
                sender_id = sender.get("user_id", sender.get("open_id", ""))
            elif isinstance(sender, str):
                sender_id = sender

            chat_id = body.get("chatid", "")
            chat_type = body.get("chat_type", "")
            is_group = chat_type == "group"
            peer_id = chat_id if chat_id else sender_id

            text = body.get("text", {}).get("content", "")
            if not text:
                return

            self._pending_frames[peer_id] = frame

            msg = InboundMessage(
                text=text,
                sender_id=sender_id,
                channel="wecom",
                peer_id=peer_id,
                is_group=is_group,
                raw=frame,
            )
            with self._q_lock:
                self._msg_queue.append(msg)

        async def _run():
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

        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._reply_async(frame, text))
            else:
                loop.run_until_complete(self._reply_async(frame, text))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._reply_async(frame, text))
            loop.close()
        return True

    async def _reply_async(self, frame: dict, text: str) -> None:
        stream_id = generate_req_id("stream")
        await self._ws_client.reply_stream(frame, stream_id, text, True)

    def _send_proactive(self, to: str, text: str) -> bool:
        """Fallback: proactive push via send_message (markdown)."""
        if not self._ws_client:
            return False
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(
                    self._ws_client.send_message(to, {
                        "msgtype": "markdown",
                        "markdown": {"content": text},
                    })
                )
            else:
                loop.run_until_complete(
                    self._ws_client.send_message(to, {
                        "msgtype": "markdown",
                        "markdown": {"content": text},
                    })
                )
        except Exception as exc:
            log.warning("wecom send_message error: %s", exc)
            return False
        return True

    def close(self) -> None:
        if self._ws_client:
            self._ws_client.disconnect()


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
            ch.close()


# ---------------------------------------------------------------------------
# Background poll loops
# ---------------------------------------------------------------------------

def wechat_poll_loop(
    wechat: WeChatChannel,
    queue: list[InboundMessage],
    lock: threading.Lock,
    stop: threading.Event,
) -> None:
    """Long-poll loop for WeChat, runs in a daemon thread."""
    print(f"  [wechat] Polling started")
    consecutive_errors = 0
    while not stop.is_set():
        if not wechat.is_logged_in:
            stop.wait(5.0)
            continue
        try:
            msgs = wechat.poll()
            consecutive_errors = 0
            if msgs:
                with lock:
                    queue.extend(msgs)
        except Exception as exc:
            consecutive_errors += 1
            wait = min(30.0, 2.0 * consecutive_errors)
            log.warning("wechat poll error: %s (retry in %.0fs)", exc, wait)
            stop.wait(wait)


def wecom_ws_loop(
    wecom: WecomChannel,
    stop: threading.Event,
) -> None:
    """WebSocket loop for WeCom, runs in a daemon thread."""
    print(f"  [wecom] WebSocket loop starting")
    wecom.start(stop)
