"""Regression coverage for :mod:`pip_agent.host_commands`.

Phase 6 scope: the dispatcher itself — recognition rules, ACL gate,
@mention handling, and a representative handful of handler happy-paths
to prove the wiring is live. Per-handler deep coverage (every edge case
of /bind, /admin subcommand matrix, etc.) is explicitly deferred to
Phase 11 so this file stays readable.

What we DO cover here:
  * A non-slash inbound is not handled.
  * An unknown slash is not handled (caller forwards to agent).
  * Leading @mention is stripped before slash detection.
  * ``/help`` / ``/status`` are open to every caller.
  * ``/admin`` is owner-only; CLI is always owner.
  * Other commands require owner OR admin.
  * Handler exceptions become ``[error] …`` responses, not crashes.
  * A handful of read-only handlers (``/help``, ``/memory``, ``/axioms``,
    ``/recall``, ``/cron``, ``/status``) return their expected shape.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pip_agent.channels import InboundMessage
from pip_agent.host_commands import (
    CommandContext,
    CommandResult,
    dispatch_command,
)
from pip_agent.routing import AgentRegistry, BindingTable


def _cli_inbound(text: str, sender_id: str = "cli-user") -> InboundMessage:
    return InboundMessage(
        text=text,
        sender_id=sender_id,
        channel="cli",
        peer_id=sender_id,
    )


def _wecom_inbound(text: str, sender_id: str = "u1") -> InboundMessage:
    return InboundMessage(
        text=text,
        sender_id=sender_id,
        channel="wecom",
        peer_id=sender_id,
    )


class _FakeMemoryStore:
    """Minimal stand-in for ``MemoryStore`` covering the methods the
    dispatcher actually calls. Each attribute is a plain value so
    tests can edit it inline to exercise different ACL configurations."""

    def __init__(
        self,
        *,
        owners: set[tuple[str, str]] | None = None,
        admins: set[tuple[str, str]] | None = None,
        stats: dict[str, Any] | None = None,
        axioms: str = "",
        recall_hits: list[dict[str, Any]] | None = None,
    ) -> None:
        self._owners = owners or set()
        self._admins = admins or set()
        self._stats = stats or {
            "agent_id": "pip-boy",
            "observations": 0,
            "memories": 0,
            "has_axioms": False,
            "axiom_lines": 0,
        }
        self._axioms = axioms
        self._recall_hits = recall_hits or []

    def is_owner(self, channel: str, sender_id: str) -> bool:
        # Mirror the real :class:`MemoryStore` contract: CLI is always
        # owner. Tests that want to exercise "wecom user is not owner"
        # simply leave ``_owners`` empty and use a wecom inbound.
        if channel == "cli":
            return True
        return (channel, sender_id) in self._owners

    def is_admin(self, channel: str, sender_id: str) -> bool:
        return (channel, sender_id) in self._admins

    def list_admins(self) -> list[str]:
        return [f"{c}:{s}" for (c, s) in sorted(self._admins)]

    def set_admin(self, name: str, *, grant: bool) -> str:
        return f"{'granted' if grant else 'revoked'} admin for {name}"

    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    def load_axioms(self) -> str:
        return self._axioms

    def search(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        assert top_k > 0
        return self._recall_hits[:top_k]


def _build_ctx(
    inbound: InboundMessage,
    tmp_path: Path,
    *,
    memory_store: _FakeMemoryStore | None = None,
    scheduler: Any | None = None,
) -> CommandContext:
    agents_dir = tmp_path / ".pip" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    registry = AgentRegistry(agents_dir=agents_dir)
    bindings = BindingTable()
    return CommandContext(
        inbound=inbound,
        registry=registry,
        bindings=bindings,
        bindings_path=agents_dir / "bindings.json",
        memory_store=memory_store,  # type: ignore[arg-type]
        scheduler=scheduler,
    )


# ---------------------------------------------------------------------------
# Recognition rules
# ---------------------------------------------------------------------------


class TestDispatchRecognition:
    def test_plain_text_is_not_handled(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("hello there"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is False
        assert result.response is None

    def test_unknown_slash_is_not_handled(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/definitely-not-a-command"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is False

    def test_leading_at_mention_is_stripped(self, tmp_path: Path):
        # WeCom @-prefix should not block slash detection. CLI gets
        # owner by virtue of channel=cli.
        ctx = _build_ctx(_cli_inbound("@bot /help"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert result.response is not None
        assert "/help" in result.response

    def test_multiple_at_mentions_are_stripped(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("@a @b /help"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True

    def test_non_string_text_is_not_handled(self, tmp_path: Path):
        # Belt-and-braces against the WeCom vision path that may pass
        # a structured payload. dispatch must not blow up.
        inbound = _cli_inbound("")
        inbound.text = [{"type": "image"}]  # type: ignore[assignment]
        ctx = _build_ctx(inbound, tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is False


# ---------------------------------------------------------------------------
# ACL gate
# ---------------------------------------------------------------------------


class TestACLGate:
    def test_cli_is_always_owner_no_memory_store(self, tmp_path: Path):
        # Fresh boot with no MemoryStore yet — CLI must still have
        # full access so the operator can run /admin before anything
        # else is initialised.
        ctx = _build_ctx(_cli_inbound("/admin list"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        # No memory_store → /admin handler itself short-circuits with
        # "Memory store unavailable." — that's acceptable; the gate
        # still let the caller through.
        assert "Memory store unavailable." in (result.response or "")

    def test_non_cli_without_memory_store_open_command_still_works(
        self, tmp_path: Path,
    ):
        ctx = _build_ctx(_wecom_inbound("/help"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "/help" in (result.response or "")

    def test_non_cli_unprivileged_rejected(self, tmp_path: Path):
        ms = _FakeMemoryStore()
        ctx = _build_ctx(_wecom_inbound("/memory"), tmp_path, memory_store=ms)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "admin" in (result.response or "").lower()

    def test_non_cli_admin_passes(self, tmp_path: Path):
        ms = _FakeMemoryStore(admins={("wecom", "u1")})
        ctx = _build_ctx(_wecom_inbound("/memory"), tmp_path, memory_store=ms)
        result = dispatch_command(ctx)
        assert result.handled is True
        # Admin passed the gate → handler ran → response is stats, not
        # a permission-denied line.
        assert "admin" not in (result.response or "").lower()

    def test_admin_blocked_from_admin_command(self, tmp_path: Path):
        # /admin is owner-only; admins are explicitly *not* allowed.
        ms = _FakeMemoryStore(admins={("wecom", "u1")})
        ctx = _build_ctx(
            _wecom_inbound("/admin list"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "owner only" in (result.response or "").lower()

    def test_non_cli_owner_passes_admin(self, tmp_path: Path):
        ms = _FakeMemoryStore(owners={("wecom", "u1")})
        ctx = _build_ctx(
            _wecom_inbound("/admin list"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "admin" in (result.response or "").lower()


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_handler_exception_becomes_error_response(
        self, tmp_path: Path, monkeypatch,
    ):
        # Replace /help with a crasher and confirm the dispatcher
        # swallows it into a [error] string rather than re-raising.
        from pip_agent import host_commands as hc

        def _boom(_ctx, _args):
            raise RuntimeError("kaboom")

        monkeypatch.setitem(hc._HANDLERS, "/help", _boom)

        ctx = _build_ctx(_cli_inbound("/help"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "[error]" in (result.response or "")
        assert "kaboom" in (result.response or "")


# ---------------------------------------------------------------------------
# Representative handler happy paths
# ---------------------------------------------------------------------------


class TestHandlerOutputs:
    def test_help_lists_commands(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/help"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        for cmd in ("/help", "/status", "/memory", "/axioms", "/cron", "/bind"):
            assert cmd in (result.response or "")

    def test_status_shows_agent_and_session(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/status"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Agent:" in body
        assert "Session:" in body
        assert "Channel: cli" in body

    def test_memory_returns_stats(self, tmp_path: Path):
        ms = _FakeMemoryStore(
            stats={
                "agent_id": "pip-boy",
                "observations": 7,
                "memories": 3,
                "has_axioms": True,
                "axiom_lines": 5,
                "last_reflect_at": 1_000_000_000.0,
            },
        )
        ctx = _build_ctx(_cli_inbound("/memory"), tmp_path, memory_store=ms)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Observations: 7" in body
        assert "Memories: 3" in body
        assert "Axioms: yes" in body
        assert "Last reflect:" in body

    def test_memory_without_store_is_friendly(self, tmp_path: Path):
        # CLI is still owner, so the gate passes; the handler itself
        # handles the missing-store case with a specific message.
        ctx = _build_ctx(_cli_inbound("/memory"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "not initialized" in (result.response or "").lower()

    def test_axioms_empty_hint(self, tmp_path: Path):
        ms = _FakeMemoryStore(axioms="")
        ctx = _build_ctx(_cli_inbound("/axioms"), tmp_path, memory_store=ms)
        result = dispatch_command(ctx)
        assert result.handled
        assert "No axioms" in (result.response or "")

    def test_axioms_returns_body(self, tmp_path: Path):
        ms = _FakeMemoryStore(axioms="- Eric values concision.")
        ctx = _build_ctx(_cli_inbound("/axioms"), tmp_path, memory_store=ms)
        result = dispatch_command(ctx)
        assert result.handled
        assert result.response == "- Eric values concision."

    def test_recall_requires_query(self, tmp_path: Path):
        ms = _FakeMemoryStore()
        ctx = _build_ctx(_cli_inbound("/recall"), tmp_path, memory_store=ms)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Usage:" in (result.response or "")

    def test_recall_returns_hits(self, tmp_path: Path):
        ms = _FakeMemoryStore(
            recall_hits=[
                {"text": "Eric prefers concise replies.", "score": 0.87},
                {"text": "Communicates in Chinese.", "score": 0.65},
            ],
        )
        ctx = _build_ctx(
            _cli_inbound("/recall concise"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Eric prefers concise replies." in body
        assert "0.87" in body

    def test_cron_no_scheduler(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/cron"), tmp_path, scheduler=None)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Scheduler not running." in (result.response or "")

    def test_cron_empty_list(self, tmp_path: Path):
        sched = SimpleNamespace(list_jobs=lambda: [])
        ctx = _build_ctx(
            _cli_inbound("/cron"), tmp_path, scheduler=sched,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "No cron jobs configured." in (result.response or "")

    def test_cron_lists_jobs(self, tmp_path: Path):
        sched = SimpleNamespace(
            list_jobs=lambda: [
                {
                    "id": "j1",
                    "name": "Daily summary",
                    "schedule_kind": "cron",
                    "enabled": True,
                    "consecutive_errors": 0,
                    "next_fire_at": 1_800_000_000.0,
                },
                {
                    "id": "j2",
                    "name": "Flaky one",
                    "schedule_kind": "at",
                    "enabled": False,
                    "consecutive_errors": 5,
                },
            ],
        )
        ctx = _build_ctx(_cli_inbound("/cron"), tmp_path, scheduler=sched)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Daily summary" in body
        assert "[on ]" in body
        assert "Flaky one" in body
        assert "[off]" in body
        assert "errors=5" in body

    def test_exit_on_cli_returns_hint(self, tmp_path: Path):
        # CLI /exit is normally intercepted *before* dispatch, but the
        # dispatcher still owns a fallback message for any caller that
        # reaches it anyway.
        ctx = _build_ctx(_cli_inbound("/exit"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "CLI" in (result.response or "")

    def test_exit_on_non_cli_is_friendly(self, tmp_path: Path):
        ms = _FakeMemoryStore(admins={("wecom", "u1")})
        ctx = _build_ctx(
            _wecom_inbound("/exit"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "CLI" in (result.response or "")


# ---------------------------------------------------------------------------
# Bind / unbind / name — enough to prove routing writes reach the store
# ---------------------------------------------------------------------------


class TestBindingMutators:
    def test_bind_creates_and_persists_binding(
        self, tmp_path: Path,
    ):
        inbound = _cli_inbound("/bind helper")
        ctx = _build_ctx(inbound, tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Bound to" in body
        # Binding table now has one entry routing cli-user → helper.
        bindings = ctx.bindings.list_all()
        assert len(bindings) == 1
        assert bindings[0].agent_id == "helper"
        assert bindings[0].match_key == "peer_id"
        # And it was persisted to disk.
        assert ctx.bindings_path.is_file()

    def test_bind_rejects_unknown_option(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/bind helper --foo bar"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Unknown option" in (result.response or "")

    def test_unbind_removes_existing_binding(self, tmp_path: Path):
        # First bind, then unbind — both through dispatch to exercise
        # the full code path.
        first = _build_ctx(_cli_inbound("/bind helper"), tmp_path)
        dispatch_command(first)
        assert len(first.bindings.list_all()) == 1

        second = CommandContext(
            inbound=_cli_inbound("/unbind"),
            registry=first.registry,
            bindings=first.bindings,
            bindings_path=first.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(second)
        assert result.handled
        assert "removed" in (result.response or "").lower()
        assert second.bindings.list_all() == []

    def test_unbind_when_no_binding(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/unbind"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "No binding" in (result.response or "")


# ---------------------------------------------------------------------------
# /admin subcommand matrix — owner-only gate already covered; here we
# verify routing to the MemoryStore ACL methods
# ---------------------------------------------------------------------------


class TestAdminSubcommands:
    def test_admin_requires_subcommand(self, tmp_path: Path):
        ms = _FakeMemoryStore()
        ctx = _build_ctx(
            _cli_inbound("/admin"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "Usage:" in (result.response or "")

    def test_admin_list_empty(self, tmp_path: Path):
        ms = _FakeMemoryStore()
        ctx = _build_ctx(
            _cli_inbound("/admin list"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "No admin users" in (result.response or "")

    def test_admin_list_populated(self, tmp_path: Path):
        ms = _FakeMemoryStore(admins={("wecom", "u1"), ("wecom", "u2")})
        ctx = _build_ctx(
            _cli_inbound("/admin list"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "wecom:u1" in body
        assert "wecom:u2" in body

    def test_admin_grant_requires_name(self, tmp_path: Path):
        ms = _FakeMemoryStore()
        ctx = _build_ctx(
            _cli_inbound("/admin grant"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "Usage:" in (result.response or "")

    def test_admin_grant_forwards_to_store(self, tmp_path: Path):
        ms = _FakeMemoryStore()
        ctx = _build_ctx(
            _cli_inbound("/admin grant alice"), tmp_path, memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "granted admin for alice" in (result.response or "")


# ---------------------------------------------------------------------------
# CommandResult shape (sanity)
# ---------------------------------------------------------------------------


def test_command_result_defaults():
    r = CommandResult(handled=False)
    assert r.response is None
    assert r.handled is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
