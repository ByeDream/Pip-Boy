"""Tests for ``AgentHost._dispatch_reply``.

Covers the heartbeat-silencing contract plus the regular CLI / remote reply
paths. ``_dispatch_reply`` is extracted as a staticmethod precisely so these
branches can be exercised without spinning up the full SDK runtime.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pip_agent.agent_host import (
    _CRON_SENDER,
    _HEARTBEAT_SENDER,
    AgentHost,
    _is_ephemeral_sender,
)
from pip_agent.agent_runner import QueryResult
from pip_agent.channels import InboundMessage


def _heartbeat(text: str = "do the check") -> InboundMessage:
    return InboundMessage(
        text=text,
        sender_id="__heartbeat__",
        channel="cli",
        peer_id="cli-user",
    )


def _cli_user(text: str = "hi") -> InboundMessage:
    return InboundMessage(
        text=text, sender_id="cli-user", channel="cli", peer_id="cli-user",
    )


def _wecom_user(text: str = "hi") -> InboundMessage:
    return InboundMessage(
        text=text, sender_id="u-123", channel="wecom", peer_id="u-123",
    )


def _cron(text: str = "daily check") -> InboundMessage:
    return InboundMessage(
        text=text, sender_id="__cron__", channel="cli", peer_id="cli-user",
    )


class TestHeartbeatSentinelSilencing:
    """`HEARTBEAT_OK` is the "nothing to report" sentinel (see
    ``scaffold/heartbeat.md``). Only that exact reply is silenced; anything
    substantive — proactive greetings, reminders, alerts — flows through the
    normal dispatch path so the user actually sees it.
    """

    @pytest.mark.parametrize("text", [
        "HEARTBEAT_OK",
        "heartbeat_ok",
        "  HEARTBEAT_OK  ",
        "HEARTBEAT_OK.",
        "`HEARTBEAT_OK`",
        '"HEARTBEAT_OK"',
        "HEARTBEAT OK",
        "Heartbeat-Ok",
    ])
    def test_sentinel_variants_are_swallowed(self, text, capsys, caplog):
        caplog.set_level("INFO")

        AgentHost._dispatch_reply(
            inbound=_heartbeat(),
            result=QueryResult(text=text),
            ch=None,
            reply_peer="cli-user",
            session_key="k",
        )
        assert capsys.readouterr().out == "", f"expected silent stdout for {text!r}"
        assert any("heartbeat sentinel" in r.message.lower() for r in caplog.records)

    def test_substantive_heartbeat_reply_goes_to_cli(self, capsys):
        # A heartbeat saying "hey, you have 3 uncommitted files" must NOT be
        # silenced — that is the whole value of heartbeats. ``process_inbound``
        # calls ``run_query`` with ``stream_text=False`` for heartbeats so the
        # sentinel can be post-filtered, which means dispatch is the sole
        # source of heartbeat output and must print the full text itself.
        AgentHost._dispatch_reply(
            inbound=_heartbeat(),
            result=QueryResult(text="You have 3 uncommitted files on main."),
            ch=None,
            reply_peer="cli-user",
            session_key="k",
        )
        out = capsys.readouterr().out
        assert "3 uncommitted files" in out

    def test_heartbeat_reply_with_ok_as_substring_is_not_swallowed(self, capsys):
        # Word "ok" appearing inside a real message must not match the sentinel.
        AgentHost._dispatch_reply(
            inbound=_heartbeat(),
            result=QueryResult(text="Everything ok, but HEARTBEAT_OK? no, say hi."),
            ch=None,
            reply_peer="cli-user",
            session_key="k",
        )
        out = capsys.readouterr().out
        assert "say hi" in out

    def test_heartbeat_sentinel_not_sent_to_remote_channel(self, monkeypatch):
        sent: list[tuple[str, str]] = []

        def _fake_send(ch, peer, text):
            sent.append((peer, text))
            return True

        from pip_agent import agent_host
        monkeypatch.setattr(agent_host, "send_with_retry", _fake_send)

        inbound = InboundMessage(
            text="do the check",
            sender_id="__heartbeat__",
            channel="wecom",
            peer_id="u-123",
        )
        AgentHost._dispatch_reply(
            inbound=inbound,
            result=QueryResult(text="HEARTBEAT_OK"),
            ch=MagicMock(),
            reply_peer="u-123",
            session_key="k",
        )
        assert sent == []

    def test_substantive_heartbeat_reply_goes_to_remote_channel(self, monkeypatch):
        sent: list[tuple[str, str]] = []

        def _fake_send(ch, peer, text):
            sent.append((peer, text))
            return True

        from pip_agent import agent_host
        monkeypatch.setattr(agent_host, "send_with_retry", _fake_send)

        inbound = InboundMessage(
            text="do the check",
            sender_id="__heartbeat__",
            channel="wecom",
            peer_id="u-123",
        )
        AgentHost._dispatch_reply(
            inbound=inbound,
            result=QueryResult(text="Good morning! Any blockers today?"),
            ch=MagicMock(),
            reply_peer="u-123",
            session_key="k",
        )
        assert sent == [("u-123", "Good morning! Any blockers today?")]

    def test_heartbeat_error_is_reported_not_suppressed(self, capsys):
        # Errors during a heartbeat are a real signal — let them through.
        AgentHost._dispatch_reply(
            inbound=_heartbeat(),
            result=QueryResult(error="boom"),
            ch=None,
            reply_peer="cli-user",
            session_key="k",
        )
        out = capsys.readouterr().out
        assert "[error]" in out and "boom" in out


class TestCliReply:
    def test_user_text_terminates_line_without_reprinting(self, capsys):
        # Non-heartbeat CLI text is streamed live by ``agent_runner`` via
        # ``TextBlock`` — by the time ``_dispatch_reply`` runs, every
        # character is already on stdout. Re-printing ``result.text`` here
        # would show the reply twice. Dispatch therefore only emits a
        # terminating newline so the next ``>>>`` prompt starts on a fresh
        # line. This test locks that contract: *don't* re-print, *do*
        # produce a newline.
        AgentHost._dispatch_reply(
            inbound=_cli_user(),
            result=QueryResult(text="Hello!"),
            ch=None,
            reply_peer="cli-user",
            session_key="k",
        )
        out = capsys.readouterr().out
        assert "Hello!" not in out, (
            "dispatch must not re-print streamed text — streaming happens "
            "upstream in agent_runner"
        )
        assert out == "\n", f"expected a lone newline, got {out!r}"

    def test_user_error_reaches_stdout(self, capsys):
        AgentHost._dispatch_reply(
            inbound=_cli_user(),
            result=QueryResult(error="kaboom"),
            ch=None,
            reply_peer="cli-user",
            session_key="k",
        )
        out = capsys.readouterr().out
        assert "[error]" in out and "kaboom" in out


class TestRemoteChannel:
    def test_user_text_is_sent_via_channel(self, monkeypatch):
        sent: list[tuple[str, str]] = []

        def _fake_send(ch, peer, text):
            sent.append((peer, text))
            return True

        from pip_agent import agent_host
        monkeypatch.setattr(agent_host, "send_with_retry", _fake_send)

        AgentHost._dispatch_reply(
            inbound=_wecom_user(),
            result=QueryResult(text="hi back"),
            ch=MagicMock(),
            reply_peer="u-123",
            session_key="k",
        )
        assert sent == [("u-123", "hi back")]


class TestCronNotSilenced:
    def test_cron_text_does_not_reprint_streamed_content(self, capsys):
        # Cron inbounds go through ``run_query`` with streaming enabled (same
        # path as a regular user message — only heartbeats disable stream).
        # Dispatch therefore must NOT re-print text but must still emit a
        # trailing newline so cron output doesn't collide with the next
        # prompt. Silencing cron entirely would defeat the whole point of
        # the scheduler.
        AgentHost._dispatch_reply(
            inbound=_cron(),
            result=QueryResult(text="Daily report ready"),
            ch=None,
            reply_peer="cli-user",
            session_key="k",
        )
        out = capsys.readouterr().out
        assert "Daily report ready" not in out, (
            "cron text was streamed upstream; re-printing here duplicates it"
        )
        assert out == "\n"

    def test_cron_text_goes_through_remote_channel(self, monkeypatch):
        sent: list[tuple[str, str]] = []

        def _fake_send(ch, peer, text):
            sent.append((peer, text))
            return True

        from pip_agent import agent_host
        monkeypatch.setattr(agent_host, "send_with_retry", _fake_send)

        # Cron inbound configured for wecom.
        inbound = InboundMessage(
            text="daily",
            sender_id="__cron__",
            channel="wecom",
            peer_id="u-456",
        )
        AgentHost._dispatch_reply(
            inbound=inbound,
            result=QueryResult(text="Report"),
            ch=MagicMock(),
            reply_peer="u-456",
            session_key="k",
        )
        assert sent == [("u-456", "Report")]


class TestEmptyResult:
    @pytest.mark.parametrize("inbound", [_cli_user(), _heartbeat(), _cron()])
    def test_no_text_no_error_is_noop(self, inbound, capsys):
        AgentHost._dispatch_reply(
            inbound=inbound,
            result=QueryResult(),
            ch=None,
            reply_peer="cli-user",
            session_key="k",
        )
        assert capsys.readouterr().out == ""


class TestIsEphemeralSender:
    """Regression lock on SDK-session opt-out for scheduler senders.

    Flipping either of these to ``False`` reintroduces the bug where
    every 30 s cron tick re-ships the full user transcript to the API
    and then appends its own ``打印 hello`` back into it, turning a
    10 s cold start into a 3 min one over a day of use. If a future
    refactor needs to make cron / heartbeat stateful it MUST solve
    the transcript-bloat problem first — this test is the tripwire.
    """

    def test_cron_sender_is_ephemeral(self):
        assert _is_ephemeral_sender(_CRON_SENDER) is True

    def test_heartbeat_sender_is_ephemeral(self):
        assert _is_ephemeral_sender(_HEARTBEAT_SENDER) is True

    @pytest.mark.parametrize(
        "sender",
        ["cli-user", "wechat:alice", "wecom:bob", "", "random-string"],
    )
    def test_everything_else_keeps_session(self, sender):
        assert _is_ephemeral_sender(sender) is False
