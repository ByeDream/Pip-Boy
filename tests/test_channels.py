"""Tests for the channel abstraction layer."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pip_agent.channels import (
    ChannelManager,
    CLIChannel,
    InboundMessage,
    WeChatChannel,
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
# WeChatChannel — credential persistence
# ---------------------------------------------------------------------------

class TestWeChatCredentials:
    @pytest.fixture
    def state_dir(self, tmp_path):
        return tmp_path / ".pip"

    def test_save_and_load_creds(self, state_dir):
        ch = WeChatChannel(state_dir)
        ch._bot_token = "ilinkbot_test123"
        ch._base_url = "https://ilinkai.weixin.qq.com"
        ch._account_id = "bot@im.bot"
        ch._user_id = "user@im.wechat"
        ch._get_updates_buf = "eyJ0ZXN0IjoxfQ=="
        ch._save_creds()

        cred_path = state_dir / "wechat_session.json"
        assert cred_path.exists()

        data = json.loads(cred_path.read_text("utf-8"))
        assert data["token"] == "ilinkbot_test123"
        assert data["accountId"] == "bot@im.bot"

        ch2 = WeChatChannel(state_dir)
        assert ch2._bot_token == "ilinkbot_test123"
        assert ch2._account_id == "bot@im.bot"
        assert ch2._get_updates_buf == "eyJ0ZXN0IjoxfQ=="
        assert ch2.is_logged_in is True

    def test_clear_creds(self, state_dir):
        ch = WeChatChannel(state_dir)
        ch._bot_token = "token"
        ch._save_creds()
        assert ch.is_logged_in is True

        ch._clear_creds()
        assert ch.is_logged_in is False
        assert not (state_dir / "wechat_session.json").exists()


# ---------------------------------------------------------------------------
# WeChatChannel — getupdates parsing
# ---------------------------------------------------------------------------

class TestWeChatPoll:
    @pytest.fixture
    def wechat(self, tmp_path):
        ch = WeChatChannel(tmp_path / ".pip")
        ch._bot_token = "token123"
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
                        {"type": 1, "text_item": {"text": "你好"}}
                    ],
                }
            ],
            "get_updates_buf": "new_buf_value",
        }

        with patch.object(wechat._http, "post", return_value=mock_response):
            results = wechat.poll()

        assert len(results) == 1
        msg = results[0]
        assert msg.text == "你好"
        assert msg.sender_id == "user@im.wechat"
        assert msg.channel == "wechat"
        assert msg.peer_id == "user@im.wechat"
        assert wechat._context_tokens["user@im.wechat"] == "ctx_abc123"
        assert wechat._get_updates_buf == "new_buf_value"

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
                }
            ],
            "get_updates_buf": "",
        }

        with patch.object(wechat._http, "post", return_value=mock_response):
            results = wechat.poll()

        assert len(results) == 0

    def test_session_expired(self, wechat):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ret": -14,
            "errcode": -14,
            "errmsg": "session timeout",
        }

        with patch.object(wechat._http, "post", return_value=mock_response):
            results = wechat.poll()

        assert len(results) == 0
        assert wechat._bot_token == ""


# ---------------------------------------------------------------------------
# WeChatChannel — send
# ---------------------------------------------------------------------------

class TestWeChatSend:
    @pytest.fixture
    def wechat(self, tmp_path):
        ch = WeChatChannel(tmp_path / ".pip")
        ch._bot_token = "token123"
        ch._context_tokens["user@im.wechat"] = "ctx_token"
        return ch

    def test_send_with_context_token(self, wechat):
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(wechat._http, "post", return_value=mock_response) as mock_post:
            ok = wechat.send("user@im.wechat", "Hello!")

        assert ok is True
        call_args = mock_post.call_args
        body = call_args.kwargs.get("json", call_args[1].get("json", {}))
        assert body["msg"]["context_token"] == "ctx_token"
        assert body["msg"]["message_type"] == 2
        assert body["msg"]["item_list"][0]["text_item"]["text"] == "Hello!"

    def test_send_without_context_token(self, wechat):
        ok = wechat.send("unknown_user", "Hello!")
        assert ok is False

    def test_has_context_token(self, wechat):
        assert wechat.has_context_token("user@im.wechat") is True
        assert wechat.has_context_token("nobody") is False
