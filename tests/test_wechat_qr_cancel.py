"""Cancellable QR login contract for :class:`WeChatChannel`.

The old ``login()`` did a 40-second long-poll per iteration which
made the CLI unresponsive during a scan — ``/exit`` and
``/wechat cancel`` had to wait out the current HTTP call before
anything else could run.

The new ``login(stop, cancel)`` caps each HTTP call at ~10 s and
checks both events between polls via ``stop.wait(1.0)``, so
cancellation is observed within ~1 s. These tests lock that down
using ``httpx.MockTransport`` so we exercise the real HTTP layer
(no ``patch.object(ch._http, ...)`` monkeying) and can count the
exact number of requests made.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from pip_agent.channels.wechat import WeChatChannel


def _mock_transport_factory() -> tuple[httpx.MockTransport, dict[str, int]]:
    """Transport that counts requests by endpoint.

    * ``GET /ilink/bot/get_bot_qrcode`` → QR id + URL (returned once).
    * ``GET /ilink/bot/get_qrcode_status`` → always ``wait`` (the
      login loop never sees ``confirmed`` and ends up polling until
      ``cancel`` / ``stop`` flips).
    """
    counts: dict[str, int] = {"qrcode": 0, "status": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_bot_qrcode"):
            counts["qrcode"] += 1
            return httpx.Response(
                200, json={
                    "qrcode": "qr-id",
                    "qrcode_img_content": "https://example/qr",
                },
            )
        if request.url.path.endswith("/get_qrcode_status"):
            counts["status"] += 1
            return httpx.Response(200, json={"status": "wait"})
        return httpx.Response(404)

    return httpx.MockTransport(_handler), counts


class TestLoginCancellable:
    def _make_channel(self, tmp_path: Path) -> tuple[
        WeChatChannel, dict[str, int],
    ]:
        ch = WeChatChannel(tmp_path / ".pip")
        transport, counts = _mock_transport_factory()
        # Swap in a fresh client backed by our mock transport so the
        # real HTTP layer runs but no network happens.
        ch._http.close()
        ch._http = httpx.Client(transport=transport, timeout=10.0)
        return ch, counts

    def test_cancel_event_exits_within_two_seconds(
        self, tmp_path: Path,
    ) -> None:
        ch, counts = self._make_channel(tmp_path)

        stop = threading.Event()
        cancel = threading.Event()

        result: list = []

        def _runner() -> None:
            with patch("qrcode.QRCode"):  # skip terminal rendering
                result.append(
                    ch.login(stop, cancel, deadline_sec=300.0),
                )

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

        # Let the worker enter at least one status poll, then cancel.
        time.sleep(0.2)
        cancel.set()
        t.join(timeout=3.0)

        assert not t.is_alive(), (
            "login() must return within a few seconds after cancel — "
            "short HTTP timeouts + stop.wait(1.0) should guarantee it."
        )
        assert result == [None]
        # We expect the loop to exit long before it has chased down
        # the 300-second deadline. A single QR request and a handful
        # of status polls is plenty; the important part is we
        # *returned*, not the exact count.
        assert counts["qrcode"] == 1

    def test_stop_event_also_aborts(self, tmp_path: Path) -> None:
        ch, _ = self._make_channel(tmp_path)

        stop = threading.Event()
        cancel = threading.Event()
        result: list = []

        def _runner() -> None:
            with patch("qrcode.QRCode"):
                result.append(
                    ch.login(stop, cancel, deadline_sec=300.0),
                )

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        time.sleep(0.2)
        stop.set()
        t.join(timeout=3.0)

        assert not t.is_alive()
        assert result == [None]

    def test_confirmed_returns_account_without_mutating_channel(
        self, tmp_path: Path,
    ) -> None:
        # Override the status handler to return ``confirmed`` on the
        # first call, and assert that ``login`` produces a fresh
        # ``_WeChatAccount`` but doesn't auto-register it (the caller
        # — today :meth:`WeChatController._qr_worker` — is responsible
        # for ``add_account``).
        ch = WeChatChannel(tmp_path / ".pip")

        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/get_bot_qrcode"):
                return httpx.Response(
                    200, json={
                        "qrcode": "qr",
                        "qrcode_img_content": "https://example/qr",
                    },
                )
            return httpx.Response(
                200, json={
                    "status": "confirmed",
                    "ilink_bot_id": "bot-new",
                    "bot_token": "token-new",
                    "baseurl": "https://ilinkai.weixin.qq.com",
                    "ilink_user_id": "uid",
                },
            )

        ch._http.close()
        ch._http = httpx.Client(
            transport=httpx.MockTransport(_handler), timeout=10.0,
        )
        stop = threading.Event()
        cancel = threading.Event()
        with patch("qrcode.QRCode"):
            acc = ch.login(stop, cancel, deadline_sec=30.0)

        assert acc is not None
        assert acc.account_id == "bot-new"
        assert acc.bot_token == "token-new"
        # login() must NOT register the account on the channel —
        # that's the caller's job.
        assert ch.get_account("bot-new") is None
