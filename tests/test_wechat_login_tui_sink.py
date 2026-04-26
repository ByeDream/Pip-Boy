"""WeChat QR login must reach the TUI agent pane, not raw ``print``."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from pip_agent import host_io
from pip_agent.channels.wechat import WeChatChannel
from pip_agent.tui.messages import AgentMessage
from pip_agent.tui.pump import UiPump


def _transport() -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_bot_qrcode"):
            return httpx.Response(
                200,
                json={
                    "qrcode": "qr-id",
                    "qrcode_img_content": "https://example.invalid/qr",
                },
            )
        if request.url.path.endswith("/get_qrcode_status"):
            return httpx.Response(200, json={"status": "wait"})
        return httpx.Response(404)

    return httpx.MockTransport(_handler)


def test_login_emits_markdown_qr_block_when_tui_active(tmp_path: Path) -> None:
    class RecApp:
        def __init__(self) -> None:
            self.messages: list[object] = []

        def post_message(self, msg: object) -> None:
            self.messages.append(msg)

    pump = UiPump()
    rec_app = RecApp()
    pump.attach(rec_app)
    host_io.install_pump(pump)
    try:
        ch = WeChatChannel(tmp_path / ".pip")
        ch._http.close()
        ch._http = httpx.Client(transport=_transport(), timeout=10.0)

        stop = threading.Event()
        cancel = threading.Event()

        def _runner() -> None:
            ch.login(stop, cancel, deadline_sec=1.0)

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        time.sleep(0.25)
        cancel.set()
        t.join(timeout=3.0)

        md_chunks = [
            m.event.text
            for m in rec_app.messages
            if isinstance(m, AgentMessage) and m.event.kind == "markdown"
        ]
        blob = "\n".join(md_chunks)
        assert "### WeChat" in blob
        assert "```text" in blob
        assert "https://example.invalid/qr" in blob
    finally:
        host_io.uninstall_pump()
