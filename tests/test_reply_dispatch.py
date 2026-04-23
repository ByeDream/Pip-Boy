"""Tests for ``AgentHost._dispatch_reply``.

Covers the heartbeat-silencing contract plus the regular CLI / remote reply
paths. ``_dispatch_reply`` is extracted as a staticmethod precisely so these
branches can be exercised without spinning up the full SDK runtime.
"""

from __future__ import annotations

from pathlib import Path
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

        def _fake_send(ch, peer, text, **kw):
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

        def _fake_send(ch, peer, text, **kw):
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

        def _fake_send(ch, peer, text, **kw):
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

        def _fake_send(ch, peer, text, **kw):
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


class TestReapStaleSession:
    """Regression: deleting a session JSONL must not fatal the next turn.

    Before this check, passing a dead session id into
    ``run_query(resume=...)`` made the CC subprocess exit 1, which the
    SDK surfaced as ``ClaudeSDKError: Command failed with exit code 1``
    and the user saw a fatal error the moment they typed anything after
    hand-deleting a stale JSONL (or after CC's ``/clear``). Self-heal
    contract: the id silently drops, next turn starts fresh.
    """

    def _host(self, sessions: dict[str, str]) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(_sessions=dict(sessions))

    def test_live_session_is_returned_untouched(self, monkeypatch, tmp_path):
        import pip_agent.agent_host as mod

        jsonl = tmp_path / "live.jsonl"
        jsonl.write_text("{}", "utf-8")
        monkeypatch.setattr(mod, "locate_session_jsonl", lambda sid, **_kw: jsonl)
        save_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "_save_sessions", lambda s: save_calls.append(dict(s)),
        )

        host = self._host({"agent:pip:cli:peer:u": "live-uuid"})
        result = AgentHost._reap_stale_session(host, "agent:pip:cli:peer:u")

        assert result == "live-uuid"
        assert host._sessions == {"agent:pip:cli:peer:u": "live-uuid"}
        assert save_calls == []  # no persistence churn on the happy path

    def test_missing_session_is_dropped_and_persisted(
        self, monkeypatch, caplog,
    ):
        import pip_agent.agent_host as mod

        monkeypatch.setattr(mod, "locate_session_jsonl", lambda sid, **_kw: None)
        save_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "_save_sessions", lambda s: save_calls.append(dict(s)),
        )

        host = self._host({
            "agent:pip:cli:peer:u": "dead-uuid",
            "agent:pip:cli:peer:other": "keep-uuid",
        })

        with caplog.at_level("WARNING", logger="pip_agent.agent_host"):
            result = AgentHost._reap_stale_session(host, "agent:pip:cli:peer:u")

        assert result is None
        assert host._sessions == {"agent:pip:cli:peer:other": "keep-uuid"}
        assert save_calls == [{"agent:pip:cli:peer:other": "keep-uuid"}]
        assert any(
            "missing on disk" in rec.message for rec in caplog.records
        )

    def test_no_session_in_map_is_noop(self, monkeypatch):
        import pip_agent.agent_host as mod

        def _boom(_sid):  # locate must not be called if there's no id
            raise AssertionError("locate_session_jsonl should be skipped")

        monkeypatch.setattr(mod, "locate_session_jsonl", _boom)
        save_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "_save_sessions", lambda s: save_calls.append(dict(s)),
        )

        host = self._host({})
        result = AgentHost._reap_stale_session(host, "agent:pip:cli:peer:u")

        assert result is None
        assert save_calls == []


class TestSessionLockMap:
    """Regression: same-session messages must serialize, different sessions
    must not interfere.

    The group-chat failure mode the lock fixes: members A and B reply to
    the bot at the same instant; both resolve to the same
    ``agent:pip:wecom:peer:<gid>`` session key; their turns interleave;
    both resume the same SDK ``session_id``; the one that writes back
    second wins and the other's turn is silently lost.

    Testing the full interleave is expensive; what we can cheaply lock
    in is the *mechanism*: two requests for the same key get the same
    lock instance, two requests for different keys get distinct locks,
    and the lock dict doesn't explode under repeated hits on the same
    key. Actual mutual exclusion is an ``asyncio.Lock`` guarantee —
    not ours to re-prove.
    """

    def _host(self):
        from types import SimpleNamespace

        return SimpleNamespace(_session_locks={})

    def test_same_key_returns_the_same_lock(self):
        host = self._host()
        a = AgentHost._get_session_lock(host, "sk-1")
        b = AgentHost._get_session_lock(host, "sk-1")
        assert a is b

    def test_different_keys_get_distinct_locks(self):
        host = self._host()
        a = AgentHost._get_session_lock(host, "sk-1")
        b = AgentHost._get_session_lock(host, "sk-2")
        assert a is not b

    def test_lock_dict_does_not_grow_on_repeat(self):
        host = self._host()
        for _ in range(10):
            AgentHost._get_session_lock(host, "sk-1")
        assert len(host._session_locks) == 1

    def test_lock_is_an_asyncio_lock(self):
        import asyncio

        host = self._host()
        lock = AgentHost._get_session_lock(host, "sk-1")
        assert isinstance(lock, asyncio.Lock)


class TestAgentIdFromSessionKey:
    """Session-key → agent_id inverse used by flush_and_rotate."""

    def test_simple_peer_key(self):
        from pip_agent.agent_host import _agent_id_from_session_key

        assert _agent_id_from_session_key(
            "agent:pip-boy:cli:peer:cli-user"
        ) == "pip-boy"

    def test_group_scoped_key(self):
        from pip_agent.agent_host import _agent_id_from_session_key

        assert _agent_id_from_session_key(
            "agent:pip-boy:wecom:guild:g1:peer:u1"
        ) == "pip-boy"

    def test_main_scope_key(self):
        from pip_agent.agent_host import _agent_id_from_session_key

        assert _agent_id_from_session_key(
            "agent:pip-boy:wecom:main"
        ) == "pip-boy"

    def test_malformed_returns_empty(self):
        from pip_agent.agent_host import _agent_id_from_session_key

        assert _agent_id_from_session_key("") == ""
        assert _agent_id_from_session_key("not-a-session-key") == ""
        assert _agent_id_from_session_key("agent:") == ""


class TestFlushAndRotate:
    """On-exit reflect + session rotation.

    Contract: reflect every live session, persist observations, then clear
    the in-memory map so the next launch mints fresh session ids.
    """

    def _build_host(self, tmp_path: Path, sessions: dict[str, str]):
        """Minimal AgentHost assembled by hand — same trick as the other
        test classes in this module. Avoids spinning a ChannelManager /
        HostScheduler that ``flush_and_rotate`` doesn't touch anyway.
        """
        from types import SimpleNamespace

        from pip_agent import agent_host as mod
        from pip_agent.memory import MemoryStore

        pip_dir = tmp_path / ".pip"
        pip_dir.mkdir(parents=True)
        mem = MemoryStore(
            agent_dir=pip_dir,
            workspace_pip_dir=pip_dir,
            agent_id="pip-boy",
        )

        # ``paths`` mirrors the v2 :class:`AgentPaths` surface used by
        # ``flush_and_rotate`` — we only need ``cwd`` to satisfy the
        # ``locate_session_jsonl`` call, the rest stays unread.
        fake_paths = SimpleNamespace(
            agent_id="pip-boy",
            cwd=tmp_path,
            pip_dir=pip_dir,
            workspace_pip_dir=pip_dir,
        )
        # ``flush_and_rotate`` now asks the registry whether the agent
        # still exists before materialising its services, to avoid
        # resurrecting a ``.pip/`` that ``/agent delete`` just wiped.
        # A tiny stub that always returns "yes" keeps the happy-path
        # tests honest without dragging the real AgentRegistry in.
        fake_registry = SimpleNamespace(
            get_agent=lambda aid: SimpleNamespace(id=aid),
        )
        host = SimpleNamespace(
            _sessions=dict(sessions),
            _agents={},
            _registry=fake_registry,
            _get_agent_services=lambda aid: SimpleNamespace(
                memory_store=mem, paths=fake_paths,
            ),
        )
        return host, mem, mod

    def test_noop_when_no_sessions(self, tmp_path: Path):
        import asyncio

        host, _, _ = self._build_host(tmp_path, sessions={})

        # Should return without touching anything — no network calls,
        # no state writes. Running under asyncio.run also confirms we're
        # actually awaitable. The empty summary is what the CLI uses to
        # pick the "Powering down." (no reflect, no rotation) branch.
        summary = asyncio.run(AgentHost.flush_and_rotate(host))

        assert host._sessions == {}
        assert summary.rotated == 0
        assert summary.reflected == 0
        assert summary.observations == 0

    def test_reflect_then_rotate_happy_path(
        self, tmp_path: Path, monkeypatch,
    ):
        import asyncio
        from unittest.mock import MagicMock

        sk = "agent:pip-boy:cli:peer:cli-user"
        host, mem, mod = self._build_host(tmp_path, sessions={sk: "sid-123"})

        save_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "_save_sessions", lambda s: save_calls.append(dict(s)),
        )
        monkeypatch.setattr(
            mod, "locate_session_jsonl",
            lambda _sid, **_kw: tmp_path / "fake.jsonl",
        )
        monkeypatch.setattr(
            "pip_agent.anthropic_client.build_anthropic_client",
            lambda: object(),
        )
        fake_persist = MagicMock(return_value=(0, 100, 3))
        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_and_persist", fake_persist,
        )

        summary = asyncio.run(AgentHost.flush_and_rotate(host))

        fake_persist.assert_called_once()
        kwargs = fake_persist.call_args.kwargs
        assert kwargs["session_id"] == "sid-123"
        assert kwargs["memory_store"] is mem

        # Rotation: sessions map must be empty AND persisted empty.
        assert host._sessions == {}
        assert save_calls and save_calls[-1] == {}

        # Summary reflects what actually happened — one session rotated,
        # one reflect call made, 3 observations persisted.
        assert summary.rotated == 1
        assert summary.reflected == 1
        assert summary.observations == 3

    def test_missing_transcript_still_rotates(
        self, tmp_path: Path, monkeypatch,
    ):
        import asyncio
        from unittest.mock import MagicMock

        sk = "agent:pip-boy:cli:peer:cli-user"
        host, _, mod = self._build_host(tmp_path, sessions={sk: "sid-123"})

        save_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "_save_sessions", lambda s: save_calls.append(dict(s)),
        )
        monkeypatch.setattr(mod, "locate_session_jsonl", lambda _sid, **_kw: None)
        monkeypatch.setattr(
            "pip_agent.anthropic_client.build_anthropic_client",
            lambda: object(),
        )
        fake_persist = MagicMock()
        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_and_persist", fake_persist,
        )

        summary = asyncio.run(AgentHost.flush_and_rotate(host))

        fake_persist.assert_not_called()
        assert host._sessions == {}
        assert save_calls and save_calls[-1] == {}
        # Transcript gone: session is rotated but reflect did not run,
        # so the CLI will print the "rotated N (reflect skipped)" line.
        assert summary.rotated == 1
        assert summary.reflected == 0
        assert summary.observations == 0

    def test_reflect_exception_does_not_block_rotation(
        self, tmp_path: Path, monkeypatch,
    ):
        """A reflect crash on one session must still rotate all sessions.

        The user typed /exit. Our job is to hand control back — not to
        die on some unrelated LLM transport hiccup.
        """
        import asyncio

        sk1 = "agent:pip-boy:cli:peer:user-a"
        sk2 = "agent:pip-boy:cli:peer:user-b"
        host, _, mod = self._build_host(
            tmp_path, sessions={sk1: "sid-A", sk2: "sid-B"},
        )

        save_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "_save_sessions", lambda s: save_calls.append(dict(s)),
        )
        monkeypatch.setattr(
            mod, "locate_session_jsonl",
            lambda _sid, **_kw: tmp_path / "x.jsonl",
        )
        monkeypatch.setattr(
            "pip_agent.anthropic_client.build_anthropic_client",
            lambda: object(),
        )

        calls: list[str] = []

        def _persist(*, memory_store, session_id, transcript_path, client,
                     model=""):
            calls.append(session_id)
            if session_id == "sid-A":
                raise RuntimeError("simulated outage")
            return (0, 50, 1)

        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_and_persist", _persist,
        )

        summary = asyncio.run(AgentHost.flush_and_rotate(host))

        # Both sessions were attempted (one failed, one succeeded).
        assert set(calls) == {"sid-A", "sid-B"}
        # Rotation happened regardless.
        assert host._sessions == {}
        assert save_calls and save_calls[-1] == {}
        # Only the healthy session counts as "reflected"; the crashed
        # one is rotated but not tallied into observations.
        assert summary.rotated == 2
        assert summary.reflected == 1
        assert summary.observations == 1

    def test_no_credentials_skips_reflect_but_still_rotates(
        self, tmp_path: Path, monkeypatch,
    ):
        import asyncio
        from unittest.mock import MagicMock

        sk = "agent:pip-boy:cli:peer:cli-user"
        host, _, mod = self._build_host(tmp_path, sessions={sk: "sid-X"})

        save_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "_save_sessions", lambda s: save_calls.append(dict(s)),
        )
        monkeypatch.setattr(
            "pip_agent.anthropic_client.build_anthropic_client",
            lambda: None,
        )
        fake_persist = MagicMock()
        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_and_persist", fake_persist,
        )

        summary = asyncio.run(AgentHost.flush_and_rotate(host))

        # Without credentials reflect is never called — but rotation
        # MUST still happen so next launch starts fresh.
        fake_persist.assert_not_called()
        assert host._sessions == {}
        assert save_calls and save_calls[-1] == {}
        # Summary distinguishes "rotated but reflect skipped" from
        # "no sessions at all" — this is the branch that drives the
        # CLI's "(reflect skipped)" status line.
        assert summary.rotated == 1
        assert summary.reflected == 0
        assert summary.observations == 0


class TestReflectAndPersist:
    """Shared state-aware wrapper — used by PreCompact, /exit, and MCP."""

    def _store(self, tmp_path: Path):
        from pip_agent.memory import MemoryStore

        pip_dir = tmp_path / ".pip"
        pip_dir.mkdir(parents=True)
        return MemoryStore(
            agent_dir=pip_dir,
            workspace_pip_dir=pip_dir,
            agent_id="pip-boy",
        )

    def test_zero_delta_no_state_write(self, tmp_path: Path, monkeypatch):
        from pip_agent.memory.reflect import (
            OFFSET_STATE_KEY,
            reflect_and_persist,
        )

        store = self._store(tmp_path)
        store.save_state({OFFSET_STATE_KEY: {"sess": 42}})

        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_from_jsonl",
            lambda *a, **kw: (42, []),  # cursor unchanged, no obs
        )

        # Drop the save_state so we can detect any spurious write.
        writes: list[dict] = []
        original_save = store.save_state
        store.save_state = lambda s: (writes.append(dict(s)), original_save(s))  # type: ignore[method-assign]

        start, end, n = reflect_and_persist(
            memory_store=store,
            session_id="sess",
            transcript_path=tmp_path / "nope.jsonl",
            client=object(),
        )

        assert (start, end, n) == (42, 42, 0)
        # The zero-delta branch must NOT call save_state — otherwise we
        # churn state.json on every idle PreCompact.
        assert writes == []

    def test_observations_persisted_and_cursor_advanced(
        self, tmp_path: Path, monkeypatch,
    ):
        from pip_agent.memory.reflect import (
            OFFSET_STATE_KEY,
            reflect_and_persist,
        )

        store = self._store(tmp_path)

        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_from_jsonl",
            lambda *a, **kw: (
                200,
                [{
                    "ts": 1.0, "text": "user prefers terse replies",
                    "category": "preference", "source": "auto",
                }],
            ),
        )

        start, end, n = reflect_and_persist(
            memory_store=store,
            session_id="sess",
            transcript_path=tmp_path / "x.jsonl",
            client=object(),
        )

        assert (start, end, n) == (0, 200, 1)
        assert store.load_state()[OFFSET_STATE_KEY]["sess"] == 200
        # Observation actually written to disk.
        assert store.load_all_observations()

    def test_failure_contract_cursor_not_advanced(
        self, tmp_path: Path, monkeypatch,
    ):
        """Q8: if ``reflect_from_jsonl`` returns (start, []) — e.g. because
        the LLM raised — we must not advance the cursor.

        The helper relies on the core's "advance-only" contract; this
        locks in that the wrapper doesn't paper over it with a stray
        state write.
        """
        from pip_agent.memory.reflect import (
            OFFSET_STATE_KEY,
            reflect_and_persist,
        )

        store = self._store(tmp_path)
        store.save_state({OFFSET_STATE_KEY: {"sess": 10}})

        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_from_jsonl",
            lambda *a, **kw: (10, []),  # transport failure simulation
        )

        start, end, n = reflect_and_persist(
            memory_store=store,
            session_id="sess",
            transcript_path=tmp_path / "x.jsonl",
            client=object(),
        )

        assert (start, end, n) == (10, 10, 0)
        # Cursor is exactly where we left it — next trigger retries.
        assert store.load_state()[OFFSET_STATE_KEY]["sess"] == 10

    def test_two_phase_commit_clears_pending_on_happy_path(
        self, tmp_path: Path, monkeypatch,
    ):
        """Happy path: the stage marker must NOT linger in state.json.

        Regression guard for H2: if the commit-phase pop is ever
        removed, the next reflect call will drain the stage a second
        time and duplicate observations forever.
        """
        from pip_agent.memory.reflect import (
            PENDING_REFLECT_KEY,
            reflect_and_persist,
        )

        store = self._store(tmp_path)

        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_from_jsonl",
            lambda *a, **kw: (
                100,
                [{
                    "ts": 1.0, "text": "o", "category": "decision",
                    "source": "auto",
                }],
            ),
        )

        reflect_and_persist(
            memory_store=store,
            session_id="sess",
            transcript_path=tmp_path / "x.jsonl",
            client=object(),
        )

        assert PENDING_REFLECT_KEY not in store.load_state()

    def test_crash_between_stage_and_commit_is_recovered_on_next_run(
        self, tmp_path: Path, monkeypatch,
    ):
        """Simulate a crash *after* stage + append but *before* the
        stage-clear save. On restart the pending bundle must be
        drained, the cursor advanced, and no double-append caused.

        Implementation detail: we manually plant a pending bundle
        (that's what a crashed prior run would have left on disk) and
        call ``reflect_and_persist`` with a no-op reflect (empty new
        delta). Post-condition: pending is gone, the cursor matches
        the staged offset, and observations from the bundle are on
        disk exactly once.
        """
        from pip_agent.memory.reflect import (
            OFFSET_STATE_KEY,
            PENDING_REFLECT_KEY,
            reflect_and_persist,
        )

        store = self._store(tmp_path)
        staged_obs = [{
            "ts": 2.0, "text": "staged before crash",
            "category": "lesson", "source": "auto",
        }]
        store.save_state({
            PENDING_REFLECT_KEY: {
                "session_id": "sess",
                "new_offset": 500,
                "observations": staged_obs,
            },
        })

        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_from_jsonl",
            lambda *a, **kw: (500, []),  # cursor matches stage → no new work
        )

        reflect_and_persist(
            memory_store=store,
            session_id="sess",
            transcript_path=tmp_path / "x.jsonl",
            client=object(),
        )

        state = store.load_state()
        assert PENDING_REFLECT_KEY not in state
        assert state[OFFSET_STATE_KEY]["sess"] == 500
        obs = store.load_all_observations()
        assert len(obs) == 1
        assert obs[0]["text"] == "staged before crash"

    def test_stage_saved_before_observations_write(
        self, tmp_path: Path, monkeypatch,
    ):
        """The pending marker must hit disk BEFORE observations are
        appended. Otherwise a crash inside write_observations would
        leave no trace for the drain to recover.
        """
        from pip_agent.memory.reflect import (
            PENDING_REFLECT_KEY,
            reflect_and_persist,
        )

        store = self._store(tmp_path)

        monkeypatch.setattr(
            "pip_agent.memory.reflect.reflect_from_jsonl",
            lambda *a, **kw: (
                300,
                [{
                    "ts": 3.0, "text": "o", "category": "decision",
                    "source": "auto",
                }],
            ),
        )

        events: list[str] = []
        original_save = store.save_state
        original_write = store.write_observations

        def traced_save(s):
            if PENDING_REFLECT_KEY in s:
                events.append("stage")
            else:
                events.append("commit")
            original_save(s)

        def traced_write(obs):
            events.append("append")
            original_write(obs)

        store.save_state = traced_save  # type: ignore[method-assign]
        store.write_observations = traced_write  # type: ignore[method-assign]

        reflect_and_persist(
            memory_store=store,
            session_id="sess",
            transcript_path=tmp_path / "x.jsonl",
            client=object(),
        )

        assert events == ["stage", "append", "commit"]
