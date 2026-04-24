"""Tests for the channel abstraction layer."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from pip_agent.channels import (
    ChannelManager,
    CLIChannel,
    InboundMessage,
    WeChatChannel,
    send_with_retry,
)

# ---------------------------------------------------------------------------
# InboundMessage
# ---------------------------------------------------------------------------

class TestInboundMessage:
    def test_defaults(self):
        m = InboundMessage(text="hello", sender_id="u1")
        assert m.text == "hello"
        assert m.sender_id == "u1"
        assert m.channel == ""
        assert m.peer_id == ""
        assert m.is_group is False
        assert m.raw == {}

    def test_full_fields(self):
        m = InboundMessage(
            text="hi", sender_id="u2", channel="wechat",
            peer_id="u2@im.wechat", is_group=False, raw={"seq": 1},
        )
        assert m.channel == "wechat"
        assert m.raw["seq"] == 1


# ---------------------------------------------------------------------------
# CLIChannel
# ---------------------------------------------------------------------------

class TestCLIChannel:
    def test_name(self):
        ch = CLIChannel()
        assert ch.name == "cli"

    def test_send(self, capsys):
        ch = CLIChannel()
        result = ch.send("cli-user", "Hello, Vault Dweller!")
        assert result is True
        captured = capsys.readouterr()
        assert "Hello, Vault Dweller!" in captured.out


# ---------------------------------------------------------------------------
# ChannelManager
# ---------------------------------------------------------------------------

class TestChannelManager:
    def test_register_and_get(self, capsys):
        mgr = ChannelManager()
        ch = CLIChannel()
        mgr.register(ch)
        assert mgr.get("cli") is ch
        assert "cli" in mgr.list_channels()
        assert mgr.get("nonexistent") is None

    def test_close_all(self):
        mgr = ChannelManager()
        mock_ch = MagicMock()
        mock_ch.name = "test"
        mgr.register(mock_ch)
        mgr.close_all()
        mock_ch.close.assert_called_once()


# ---------------------------------------------------------------------------
# WeChatChannel — credential persistence (per-account)
# ---------------------------------------------------------------------------

def _make_account(ch, account_id, *, token="tok", ctx_peer=None):
    """Test helper: register a fully-formed account on a WeChatChannel."""
    from pip_agent.channels.wechat import _WeChatAccount

    acc = _WeChatAccount(
        account_id=account_id,
        bot_token=token,
        base_url="https://ilinkai.weixin.qq.com",
        user_id="user@im.wechat",
    )
    ch.add_account(acc)
    if ctx_peer is not None:
        acc.context_tokens[ctx_peer] = f"ctx_{account_id}"
    return acc


class TestWeChatCredentials:
    @pytest.fixture
    def state_dir(self, tmp_path):
        return tmp_path / ".pip"

    def test_add_account_persists_under_credentials_wechat_dir(self, state_dir):
        ch = WeChatChannel(state_dir)
        _make_account(ch, "bot-a", token="ilinkbot_aaa")
        cred = state_dir / "credentials" / "wechat" / "bot-a.json"
        assert cred.exists(), "add_account should write per-account JSON file"
        data = json.loads(cred.read_text("utf-8"))
        assert data["token"] == "ilinkbot_aaa"
        assert data["accountId"] == "bot-a"

    def test_reload_discovers_existing_credential_files(self, state_dir):
        ch = WeChatChannel(state_dir)
        _make_account(ch, "bot-a", token="t_a")
        _make_account(ch, "bot-b", token="t_b")

        ch2 = WeChatChannel(state_dir)
        assert sorted(ch2.account_ids()) == ["bot-a", "bot-b"]
        assert ch2.get_account("bot-a").bot_token == "t_a"
        assert ch2.get_account("bot-b").bot_token == "t_b"
        assert ch2.has_any_logged_in

    def test_remove_account_deletes_file(self, state_dir):
        ch = WeChatChannel(state_dir)
        _make_account(ch, "bot-a")
        assert ch.remove_account("bot-a") is True
        assert not (state_dir / "credentials" / "wechat" / "bot-a.json").exists()
        assert ch.get_account("bot-a") is None

    def test_legacy_single_session_file_is_ignored(self, state_dir):
        # Pre-multi-account installs left behind ``wechat_session.json``.
        # The new channel must NOT read it (the host does a one-time
        # sweep to delete it; this test locks in that the channel itself
        # doesn't interpret the old format as an account either).
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "wechat_session.json").write_text(
            json.dumps({"token": "old", "accountId": "legacy"}),
            "utf-8",
        )
        ch = WeChatChannel(state_dir)
        assert ch.account_ids() == []


# ---------------------------------------------------------------------------
# WeChatChannel — getupdates parsing (per-account)
# ---------------------------------------------------------------------------

class TestWeChatPoll:
    @pytest.fixture
    def wechat(self, tmp_path):
        ch = WeChatChannel(tmp_path / ".pip")
        _make_account(ch, "bot-a")
        return ch

    def test_parse_text_message(self, wechat):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ret": 0,
            "msgs": [
                {
                    "seq": 1,
                    "message_type": 1,
                    "message_state": 2,
                    "from_user_id": "user@im.wechat",
                    "to_user_id": "bot@im.bot",
                    "context_token": "ctx_abc123",
                    "item_list": [
                        {"type": 1, "text_item": {"text": "你好"}},
                    ],
                },
            ],
            "get_updates_buf": "new_buf_value",
        }

        with patch.object(wechat._http, "post", return_value=mock_response):
            results = wechat.poll("bot-a")

        assert len(results) == 1
        msg = results[0]
        assert msg.text == "你好"
        assert msg.sender_id == "user@im.wechat"
        assert msg.channel == "wechat"
        assert msg.peer_id == "user@im.wechat"
        assert msg.account_id == "bot-a"
        acc = wechat.get_account("bot-a")
        assert acc.context_tokens["user@im.wechat"] == "ctx_abc123"
        assert acc.get_updates_buf == "new_buf_value"

    def test_poll_unknown_account_returns_empty(self, wechat):
        assert wechat.poll("not-a-real-bot") == []

    def test_skip_bot_messages(self, wechat):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ret": 0,
            "msgs": [
                {
                    "message_type": 2,
                    "message_state": 2,
                    "from_user_id": "bot@im.bot",
                    "item_list": [{"type": 1, "text_item": {"text": "reply"}}],
                },
            ],
            "get_updates_buf": "",
        }
        with patch.object(wechat._http, "post", return_value=mock_response):
            results = wechat.poll("bot-a")
        assert results == []

    def test_session_expired_clears_token(self, wechat):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ret": -14, "errcode": -14, "errmsg": "session timeout",
        }
        with patch.object(wechat._http, "post", return_value=mock_response):
            results = wechat.poll("bot-a")

        assert results == []
        acc = wechat.get_account("bot-a")
        assert acc.bot_token == ""
        assert acc.is_logged_in is False


# ---------------------------------------------------------------------------
# WeChatChannel — send (per-account)
# ---------------------------------------------------------------------------

class TestWeChatSend:
    @pytest.fixture
    def wechat(self, tmp_path):
        ch = WeChatChannel(tmp_path / ".pip")
        _make_account(ch, "bot-a", ctx_peer="user@im.wechat")
        return ch

    def test_send_with_context_token(self, wechat):
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch.object(
            wechat._http, "post", return_value=mock_response,
        ) as mock_post:
            ok = wechat.send(
                "user@im.wechat", "Hello!", account_id="bot-a",
            )

        assert ok is True
        body = mock_post.call_args.kwargs["json"]
        assert body["msg"]["context_token"] == "ctx_bot-a"
        assert body["msg"]["message_type"] == 2
        assert body["msg"]["item_list"][0]["text_item"]["text"] == "Hello!"

    def test_send_without_context_token(self, wechat):
        ok = wechat.send("unknown_user", "Hello!", account_id="bot-a")
        assert ok is False

    def test_send_unknown_account_returns_false(self, wechat):
        ok = wechat.send(
            "user@im.wechat", "Hello!", account_id="no-such-bot",
        )
        assert ok is False

    def test_single_account_fallback_when_account_id_missing(self, tmp_path):
        # Convenience: one-account hosts keep working if the caller
        # doesn't pass account_id. Multi-account hosts must always
        # pass it (see test_send_multi_account_requires_account_id).
        ch = WeChatChannel(tmp_path / ".pip")
        _make_account(ch, "solo", ctx_peer="user@im.wechat")
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch.object(ch._http, "post", return_value=mock_response):
            assert ch.send("user@im.wechat", "hi") is True

    def test_send_multi_account_requires_account_id(self, tmp_path):
        ch = WeChatChannel(tmp_path / ".pip")
        _make_account(ch, "bot-a", ctx_peer="user@im.wechat")
        _make_account(ch, "bot-b", ctx_peer="user@im.wechat")
        # No account_id + multiple accounts → refuse (no guessing).
        assert ch.send("user@im.wechat", "hi") is False

    def test_has_context_token(self, wechat):
        assert wechat.has_context_token(
            "user@im.wechat", account_id="bot-a",
        ) is True
        assert wechat.has_context_token(
            "nobody", account_id="bot-a",
        ) is False
        assert wechat.has_context_token(
            "user@im.wechat", account_id="no-such-bot",
        ) is False


# ---------------------------------------------------------------------------
# WecomChannel — pending-frames routing (plan M4)
# ---------------------------------------------------------------------------

# Import-time check: skip the whole block if the SDK isn't installed
# in the environment (CI without wecom-aibot-python-sdk).
_wecom_cls: object
try:
    from pip_agent.channels import WecomChannel as _wecom_cls
    _WECOM_AVAILABLE = True
except Exception:  # pragma: no cover
    _WECOM_AVAILABLE = False


@pytest.mark.skipif(not _WECOM_AVAILABLE, reason="wecom-aibot-python-sdk not installed")
class TestWecomPendingFrames:
    """Regression guard for the ``_pending_frames`` overwrite race.

    The old implementation keyed pending frames by ``peer_id``. Two
    concurrent inbound messages from the same peer would overwrite
    each other, and ``send()`` for the first message would end up
    threading ``reply_stream`` to the *second* frame — replying to
    the wrong user message.

    The fix keys by a uuid per inbound, stashed in
    ``InboundMessage.raw["_pip_inbound_id"]`` by ``_enqueue``. This
    test replicates the race shape directly against the pending-frames
    table since ``_enqueue`` is a closure inside ``start()``.
    """

    def _make_channel(self, monkeypatch):
        # Bypass __init__ so we don't need a real WSClient.
        ch = _wecom_cls.__new__(_wecom_cls)
        import threading
        ch._bot_id = "bot-1"
        ch._bot_secret = "secret"
        ch._msg_queue = []
        ch._q_lock = threading.Lock()
        ch._ws_client = MagicMock()
        ch._ws_loop = None
        ch._pending_frames = {}
        ch._pending_lock = threading.Lock()
        # Capture reply_async calls: which frame reply_stream was
        # handed for each send.
        recorded: list[dict] = []
        def _fake_run_async(coro):
            # close the coroutine to avoid "never awaited" warnings
            try:
                coro.close()
            except Exception:
                pass
            # ``_run_async`` returns ``(ok, result)`` so callers like
            # ``send()`` can thread a real success bool up to
            # ``send_with_retry``. The stub simulates a clean send.
            return True, None
        def _fake_reply_async(frame, text):  # pragma: no cover - unused
            pass
        ch._run_async = _fake_run_async  # type: ignore[attr-defined]
        # Patch _reply_async via monkeypatch on the unbound method to
        # record which frame we would have sent to.
        def _spy_reply_async(self, frame, text):
            recorded.append({"frame": frame, "text": text})
            async def _noop():
                return None
            return _noop()
        monkeypatch.setattr(_wecom_cls, "_reply_async", _spy_reply_async)
        return ch, recorded

    def test_concurrent_inbounds_from_same_peer_do_not_overwrite(
        self, monkeypatch,
    ):
        ch, recorded = self._make_channel(monkeypatch)

        # Simulate two back-to-back inbounds from the same peer. The
        # race in the old code happened at enqueue time — the second
        # would clobber _pending_frames[peer_id]. Here we emulate the
        # new keying contract directly.
        frame_a = {"body": {"msgtype": "text"}, "_pip_inbound_id": "id-A"}
        frame_b = {"body": {"msgtype": "text"}, "_pip_inbound_id": "id-B"}
        ch._pending_frames["id-A"] = frame_a
        ch._pending_frames["id-B"] = frame_b

        # Reply to A — must thread to frame_a, not frame_b.
        ok = ch.send("peer-1", "reply-A", inbound_id="id-A")
        assert ok is True
        assert len(recorded) == 1
        assert recorded[0]["frame"] is frame_a
        assert recorded[0]["text"] == "reply-A"

        # Reply to B — must still find frame_b; release_inbound for A
        # has no effect on B.
        ch.release_inbound("id-A")
        assert "id-A" not in ch._pending_frames
        assert ch._pending_frames["id-B"] is frame_b

        ok = ch.send("peer-1", "reply-B", inbound_id="id-B")
        assert ok is True
        assert recorded[1]["frame"] is frame_b

    def test_missing_inbound_id_falls_through_to_proactive(
        self, monkeypatch,
    ):
        """Without ``inbound_id`` (cron / heartbeat / command response
        without an originating frame), ``send`` must not raise and
        should fall through to the proactive markdown push."""
        ch, _recorded = self._make_channel(monkeypatch)
        called: list[tuple[str, str]] = []
        def _fake_proactive(to, text):
            called.append((to, text))
            return True
        monkeypatch.setattr(ch, "_send_proactive", _fake_proactive)

        ok = ch.send("peer-42", "proactive body")  # no inbound_id
        assert ok is True
        assert called == [("peer-42", "proactive body")]

    def test_send_with_retry_releases_pending_frame(self, monkeypatch):
        """After every chunk of a reply has been dispatched,
        ``send_with_retry`` calls ``release_inbound`` so the
        pending-frames table doesn't grow unbounded over the bot's
        lifetime."""
        ch, _recorded = self._make_channel(monkeypatch)
        ch._pending_frames["id-X"] = {"body": {}, "_pip_inbound_id": "id-X"}

        # Short text -> one chunk; send_with_retry must still release.
        send_with_retry(ch, "peer-x", "short reply", inbound_id="id-X")
        assert "id-X" not in ch._pending_frames

    def test_send_returns_false_when_run_async_fails(self, monkeypatch):
        """Plan-B Tier-5 maintenance: ``send`` must thread the real
        success bit from ``_run_async`` so ``send_with_retry`` can
        actually retry. Before the ``(ok, result)`` tuple refactor,
        ``send`` returned a constant ``True`` even when the WS
        coroutine failed (timeout, RuntimeError from a torn-down
        socket, arbitrary exception), making retries impossible.
        """
        ch, _recorded = self._make_channel(monkeypatch)
        ch._pending_frames["id-Y"] = {"body": {}, "_pip_inbound_id": "id-Y"}

        def _failing_run_async(coro):
            try:
                coro.close()
            except Exception:
                pass
            return False, None

        ch._run_async = _failing_run_async  # type: ignore[attr-defined]

        ok = ch.send("peer-y", "never arrives", inbound_id="id-Y")
        assert ok is False, (
            "send() must surface ``_run_async`` failure; returning "
            "True unconditionally would silently defeat send_with_retry."
        )
