"""Regression coverage for :mod:`pip_agent.host_commands`.

Phase 6 scope: the dispatcher itself — recognition rules, ACL gate,
@mention handling, and a representative handful of handler happy-paths
to prove the wiring is live. Per-handler deep coverage (every edge case
of /agent, /admin subcommand matrix, etc.) is explicitly deferred to
Phase 11 so this file stays readable.

What we DO cover here:
  * A non-slash inbound is not handled.
  * An unknown slash is rejected with a helpful error (never forwarded
    to the model — typos should fail fast).
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
    invalidate_agent: Any | None = None,
) -> CommandContext:
    # v2 layout: workspace root is the source of truth; each agent
    # has its own nested ``.pip/`` dir.
    workspace = tmp_path / "workspace"
    (workspace / ".pip").mkdir(parents=True, exist_ok=True)
    registry = AgentRegistry(workspace)
    bindings = BindingTable()
    return CommandContext(
        inbound=inbound,
        registry=registry,
        bindings=bindings,
        bindings_path=workspace / ".pip" / "bindings.json",
        memory_store=memory_store,  # type: ignore[arg-type]
        scheduler=scheduler,
        invalidate_agent=invalidate_agent,
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

    def test_unknown_slash_is_rejected_with_error(self, tmp_path: Path):
        # Strict parsing (post-v2): any ``/...`` that isn't a known
        # command must be handled with a clear error instead of being
        # silently passed to the model. This catches typos at the host
        # layer and stops them from burning an LLM turn.
        ctx = _build_ctx(_cli_inbound("/definitely-not-a-command"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        body = (result.response or "").lower()
        assert "unknown command" in body
        assert "/help" in body

    def test_typo_suggests_close_command(self, tmp_path: Path):
        # ``/agnet`` is one transposition away from ``/agent`` — we want
        # the hint surfaced so the user doesn't have to guess.
        ctx = _build_ctx(_cli_inbound("/agnet list"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "/agent" in (result.response or "")

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
        for cmd in (
            "/help", "/status", "/memory", "/axioms", "/cron",
            "/home",
            "/agent", "/agent list", "/agent create",
            "/agent switch", "/agent reset <id>",
        ):
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
# /agent <subcommand> — dispatcher + happy paths for the consolidated
# command surface (replaces the old /bind, /unbind, /switch, /reset,
# /agents, /create-agent, /archive-agent, /delete-agent).
# ---------------------------------------------------------------------------


class TestAgentCommand:
    def test_no_args_shows_current_agent(self, tmp_path: Path):
        # Bare /agent should fall into the "show detail" branch, not
        # the list/usage branch.
        ctx = _build_ctx(_cli_inbound("/agent"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Agent:" in body
        # Default agent is pip-boy — sub-agent kind should be 'root'.
        assert "Kind:" in body
        assert "Cwd:" in body

    def test_list_shows_agents(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/agent list"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "pip-boy" in body

    def test_unknown_subcommand_suggests(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/agent lst"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Unknown" in body
        # "lst" is one char off "list" — should hint.
        assert "list" in body

    def test_create_owner_only_for_remote_admin(self, tmp_path: Path):
        # An admin (non-owner) on wecom passes the top-level gate but
        # must be blocked at the per-subcommand owner check.
        ms = _FakeMemoryStore(admins={("wecom", "u1")})
        ctx = _build_ctx(
            _wecom_inbound("/agent create helper"),
            tmp_path,
            memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "owner only" in (result.response or "").lower()

    def _seed_rich_pip_boy_persona(self, tmp_path: Path):
        """Write a pip-boy persona.md that has structured guidance
        beyond ``# Identity``, then rebuild the registry so
        ``default_agent()`` reflects the richer body.

        This mirrors a real workspace where the scaffold has run and
        ``pip-boy.md`` is installed. Without it, ``_build_ctx``'s
        empty ``.pip/`` would yield the minimal ``_BUILTIN_DEFAULT``
        body and we'd have nothing to inherit from — that's a valid
        bootstrap scenario but useless for this test.
        """
        workspace = tmp_path / "workspace"
        pip_dir = workspace / ".pip"
        pip_dir.mkdir(parents=True, exist_ok=True)
        (pip_dir / "persona.md").write_text(
            "---\n"
            "name: Pip-Boy\n"
            "---\n"
            "# Identity\n\n"
            "You are Pip-Boy, a personal assistant agent.\n"
            "Your working directory is {workdir}.\n\n"
            "# Identity Recognition\n\n"
            "Each `<user_query>` carries sender metadata; the owner "
            "profile is read-only and lives at `.pip/owner.md`.\n\n"
            "# Tool Calling\n\nPrefer specialized tools.\n\n"
            "# Memory\n\nReflect after meaningful work.\n",
            encoding="utf-8",
        )
        return workspace

    def test_create_inherits_pip_boy_guidance(self, tmp_path: Path):
        """New sub-agents must ship with the full operational persona
        (Identity Recognition, Tool Calling, Memory, etc.), not just
        a 4-line identity stub. Otherwise they see the injected
        ``# User`` block in their prompt but have no framework to
        interpret it — exactly the "sub-agent doesn't know the owner"
        bug from the identity-redesign thread."""
        self._seed_rich_pip_boy_persona(tmp_path)
        ctx = _build_ctx(_cli_inbound("/agent create helper"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled, result.response

        paths = ctx.registry.paths_for("helper")
        assert paths is not None
        persona = (paths.pip_dir / "persona.md").read_text(encoding="utf-8")

        # Identity section names the new agent + references Pip-Boy
        # as the parent so the model knows the owner is shared.
        assert "You are helper" in persona
        assert "Pip-Boy" in persona
        assert "owner" in persona.lower()

        # Inherited sections survived the identity rewrite.
        for heading in ("# Identity Recognition", "# Tool Calling", "# Memory"):
            assert heading in persona, persona

    def test_create_identity_section_names_sub_agent_not_pip_boy(
        self, tmp_path: Path,
    ):
        """Only the Identity section is rewritten. The ``You are …``
        line there must name the new agent — not ``Pip-Boy``."""
        self._seed_rich_pip_boy_persona(tmp_path)
        ctx = _build_ctx(_cli_inbound("/agent create helper"), tmp_path)
        dispatch_command(ctx)

        paths = ctx.registry.paths_for("helper")
        assert paths is not None
        persona = (paths.pip_dir / "persona.md").read_text(encoding="utf-8")

        first_you_are = next(
            (line.strip() for line in persona.splitlines()
             if line.strip().startswith("You are ")),
            "",
        )
        assert first_you_are.startswith("You are helper"), first_you_are

    def test_create_and_switch_roundtrip(self, tmp_path: Path):
        # CLI → owner → /agent create succeeds, then /agent switch
        # binds the chat.
        ctx = _build_ctx(_cli_inbound("/agent create helper"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Created agent 'helper'" in (result.response or "")

        switch_ctx = CommandContext(
            inbound=_cli_inbound("/agent switch helper"),
            registry=ctx.registry,
            bindings=ctx.bindings,
            bindings_path=ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(switch_ctx)
        assert result.handled
        assert "Switched to" in (result.response or "")
        bindings = switch_ctx.bindings.list_all()
        assert len(bindings) == 1
        assert bindings[0].agent_id == "helper"
        assert switch_ctx.bindings_path.is_file()

    def test_switch_to_default_is_redirected_to_home(self, tmp_path: Path):
        # /agent switch pip-boy is no longer the way to drop a binding;
        # it must tell the operator to use /home instead so there is
        # exactly one idiom per direction.
        ctx = _build_ctx(
            _cli_inbound("/agent switch pip-boy"), tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled
        body = (result.response or "").lower()
        assert "/home" in body
        assert "not supported" in body or "is not supported" in body
        assert ctx.bindings.list_all() == []

    def test_switch_empty_lists_sub_agents_only(self, tmp_path: Path):
        # Usage hint should list *sub-agents*, not pip-boy itself.
        ctx = _build_ctx(_cli_inbound("/agent switch"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Usage:" in body
        assert "Known sub-agents:" in body
        # pip-boy is the root; it should never appear in the
        # switchable-targets list.
        known_line = next(
            line for line in body.splitlines()
            if line.startswith("Known sub-agents:")
        )
        assert "pip-boy" not in known_line

    def test_switch_unknown_agent_includes_hint(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/agent switch nosuch"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Unknown agent" in body
        assert "/agent create" in body

    def test_delete_requires_yes_flag(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/agent create helper"), tmp_path)
        dispatch_command(ctx)
        ctx2 = CommandContext(
            inbound=_cli_inbound("/agent delete helper"),
            registry=ctx.registry,
            bindings=ctx.bindings,
            bindings_path=ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(ctx2)
        assert result.handled
        assert "--yes" in (result.response or "")

    def test_delete_refuses_root(self, tmp_path: Path):
        ctx = _build_ctx(
            _cli_inbound("/agent delete pip-boy --yes"), tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "root" in (result.response or "").lower()

    def test_archive_refuses_root(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/agent archive pip-boy"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "root" in (result.response or "").lower()

    def test_delete_invalidates_host_cache(self, tmp_path: Path):
        """Regression: after ``/agent delete``, the host must drop its
        cached per-agent service and its session rows — otherwise
        ``flush_and_rotate`` on ``/exit`` reflects via the stale
        ``MemoryStore``, whose first ``save_state`` re-creates the
        wiped ``.pip/`` with a zombie ``state.json``.
        """
        called: list[str] = []
        create_ctx = _build_ctx(
            _cli_inbound("/agent create helper"),
            tmp_path,
            invalidate_agent=lambda aid: called.append(aid),
        )
        dispatch_command(create_ctx)

        delete_ctx = CommandContext(
            inbound=_cli_inbound("/agent delete helper --yes"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
            invalidate_agent=lambda aid: called.append(aid),
        )
        result = dispatch_command(delete_ctx)
        assert result.handled, result.response
        assert called == ["helper"]

    def test_archive_invalidates_host_cache(self, tmp_path: Path):
        called: list[str] = []
        create_ctx = _build_ctx(
            _cli_inbound("/agent create helper"),
            tmp_path,
            invalidate_agent=lambda aid: called.append(aid),
        )
        dispatch_command(create_ctx)

        archive_ctx = CommandContext(
            inbound=_cli_inbound("/agent archive helper"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
            invalidate_agent=lambda aid: called.append(aid),
        )
        result = dispatch_command(archive_ctx)
        assert result.handled, result.response
        assert called == ["helper"]

    def test_delete_purges_claude_code_project_dir(
        self, tmp_path: Path, monkeypatch,
    ):
        """Regression: ``/agent delete`` must also wipe CC's per-project
        cache at ``~/.claude/projects/<enc-cwd>/`` — that folder holds
        both session JSONL transcripts and CC's own ``memory/`` (the
        ``MEMORY.md`` + ``user_*.md`` cards). Without this cleanup, a
        freshly recreated agent at the same cwd rehydrates the previous
        identity's "who is my user" memory via CC's native recall,
        defeating delete.
        """
        from pip_agent.memory import transcript_source

        fake_projects_root = tmp_path / "fake_claude_projects"
        monkeypatch.setattr(
            transcript_source, "DEFAULT_PROJECTS_ROOT", fake_projects_root,
        )

        create_ctx = _build_ctx(
            _cli_inbound("/agent create helper"), tmp_path,
        )
        dispatch_command(create_ctx)

        paths = create_ctx.registry.paths_for("helper")
        assert paths is not None
        cc_dir = transcript_source.cc_project_dir_for(paths.cwd)
        (cc_dir / "memory").mkdir(parents=True)
        (cc_dir / "memory" / "MEMORY.md").write_text(
            "- [Alice](user_alice.md)", encoding="utf-8",
        )
        (cc_dir / "memory" / "user_alice.md").write_text(
            "name: Alice\nage: 35", encoding="utf-8",
        )
        (cc_dir / "some-session.jsonl").write_text("{}\n", encoding="utf-8")
        assert cc_dir.is_dir()

        delete_ctx = CommandContext(
            inbound=_cli_inbound("/agent delete helper --yes"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(delete_ctx)
        assert result.handled, result.response
        assert not cc_dir.exists()
        assert "purged CC project dir" in (result.response or "")

    def test_archive_purges_claude_code_project_dir(
        self, tmp_path: Path, monkeypatch,
    ):
        from pip_agent.memory import transcript_source

        fake_projects_root = tmp_path / "fake_claude_projects"
        monkeypatch.setattr(
            transcript_source, "DEFAULT_PROJECTS_ROOT", fake_projects_root,
        )

        create_ctx = _build_ctx(
            _cli_inbound("/agent create helper"), tmp_path,
        )
        dispatch_command(create_ctx)

        paths = create_ctx.registry.paths_for("helper")
        assert paths is not None
        cc_dir = transcript_source.cc_project_dir_for(paths.cwd)
        (cc_dir / "memory").mkdir(parents=True)
        (cc_dir / "memory" / "MEMORY.md").write_text("stale", encoding="utf-8")

        archive_ctx = CommandContext(
            inbound=_cli_inbound("/agent archive helper"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(archive_ctx)
        assert result.handled, result.response
        assert not cc_dir.exists()

    def test_agent_gated_when_on_sub_agent(self, tmp_path: Path):
        # Create a sub-agent, bind this chat to it, then verify that
        # any further /agent call (including the bare one) bounces
        # with a /home redirect — sub-agents don't manage siblings.
        create_ctx = _build_ctx(
            _cli_inbound("/agent create helper"), tmp_path,
        )
        dispatch_command(create_ctx)
        switch_ctx = CommandContext(
            inbound=_cli_inbound("/agent switch helper"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        dispatch_command(switch_ctx)

        for bad in ("/agent", "/agent list", "/agent create other"):
            ctx = CommandContext(
                inbound=_cli_inbound(bad),
                registry=create_ctx.registry,
                bindings=create_ctx.bindings,
                bindings_path=create_ctx.bindings_path,
                memory_store=None,
                scheduler=None,
            )
            result = dispatch_command(ctx)
            assert result.handled, bad
            body = (result.response or "").lower()
            assert "/home" in body, bad
            assert "pip-boy" in body, bad


# ---------------------------------------------------------------------------
# /home — leave a sub-agent and return to pip-boy
# ---------------------------------------------------------------------------


class TestHomeCommand:
    def test_home_on_pip_boy_is_noop(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/home"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = (result.response or "").lower()
        assert "already on pip-boy" in body
        assert ctx.bindings.list_all() == []

    def test_home_from_sub_agent_clears_binding(self, tmp_path: Path):
        create_ctx = _build_ctx(
            _cli_inbound("/agent create helper"), tmp_path,
        )
        dispatch_command(create_ctx)
        switch_ctx = CommandContext(
            inbound=_cli_inbound("/agent switch helper"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        dispatch_command(switch_ctx)
        assert len(create_ctx.bindings.list_all()) == 1

        home_ctx = CommandContext(
            inbound=_cli_inbound("/home"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(home_ctx)
        assert result.handled
        body = (result.response or "").lower()
        assert "back to pip-boy" in body
        assert "binding cleared" in body
        assert create_ctx.bindings.list_all() == []


# ---------------------------------------------------------------------------
# /agent reset <id> — backup · delete · rebuild · restore
# ---------------------------------------------------------------------------


class TestAgentReset:
    def test_reset_requires_id(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/agent reset"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Usage:" in (result.response or "")

    def test_reset_unknown_agent(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/agent reset nosuch"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Unknown agent" in (result.response or "")

    def test_reset_sub_agent_preserves_identity_wipes_memory(
        self, tmp_path: Path,
    ):
        # Create a sub-agent and seed its .pip/ with both an identity
        # file and a memory-layer artefact, then /agent reset.
        ctx = _build_ctx(_cli_inbound("/agent create helper"), tmp_path)
        dispatch_command(ctx)

        paths = ctx.registry.paths_for("helper")
        assert paths is not None
        pip_dir = paths.pip_dir
        # Simulate accumulated memory-layer state.
        (pip_dir / "memories.json").write_text("[\"m\"]", encoding="utf-8")
        obs = pip_dir / "observations"
        obs.mkdir(exist_ok=True)
        (obs / "2026-04-23.jsonl").write_text("{}\n", encoding="utf-8")
        # Identity should survive.
        persona = pip_dir / "persona.md"
        assert persona.is_file()
        persona_before = persona.read_text(encoding="utf-8")
        # Add a HEARTBEAT to check the second preserved slot too.
        (pip_dir / "HEARTBEAT.md").write_text("keep-me", encoding="utf-8")

        reset_ctx = CommandContext(
            inbound=_cli_inbound("/agent reset helper"),
            registry=ctx.registry,
            bindings=ctx.bindings,
            bindings_path=ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(reset_ctx)
        assert result.handled, result.response
        assert "Reset agent 'helper'" in (result.response or "")

        # Identity preserved.
        assert persona.is_file()
        assert persona.read_text(encoding="utf-8") == persona_before
        hb = pip_dir / "HEARTBEAT.md"
        assert hb.is_file() and hb.read_text(encoding="utf-8") == "keep-me"

        # Memory wiped. The ``observations/`` subdir is re-seeded empty
        # so a cached MemoryStore can still append on the next reflect.
        assert not (pip_dir / "memories.json").exists()
        obs_after = pip_dir / "observations"
        assert obs_after.is_dir() and list(obs_after.iterdir()) == []
        users_after = pip_dir / "users"
        assert users_after.is_dir() and list(users_after.iterdir()) == []

    def test_reset_pip_boy_preserves_workspace_shared_state(
        self, tmp_path: Path,
    ):
        # Set up pip-boy with identity + workspace-shared files, plus
        # memory-layer artefacts that should be wiped.
        ctx = _build_ctx(_cli_inbound("/agent reset pip-boy"), tmp_path)
        paths = ctx.registry.paths_for("pip-boy")
        assert paths is not None
        pip_dir = paths.pip_dir
        pip_dir.mkdir(parents=True, exist_ok=True)

        # Identity.
        persona = pip_dir / "persona.md"
        persona.write_text("persona-body", encoding="utf-8")
        (pip_dir / "HEARTBEAT.md").write_text("hb-body", encoding="utf-8")

        # Workspace-shared (root-only).
        (pip_dir / "owner.md").write_text("owner", encoding="utf-8")
        (pip_dir / "bindings.json").write_text("{}", encoding="utf-8")
        (pip_dir / "agents_registry.json").write_text(
            "{\"pip-boy\": {}}", encoding="utf-8",
        )
        creds = pip_dir / "credentials"
        creds.mkdir(exist_ok=True)
        (creds / "wecom.json").write_text("{}", encoding="utf-8")
        archived = pip_dir / "archived"
        archived.mkdir(exist_ok=True)
        (archived / "dummy").write_text("keep", encoding="utf-8")

        # Memory layer.
        (pip_dir / "memories.json").write_text("[]", encoding="utf-8")
        (pip_dir / "state.json").write_text("{}", encoding="utf-8")
        users = pip_dir / "users"
        users.mkdir(exist_ok=True)
        (users / "u.md").write_text("u", encoding="utf-8")

        result = dispatch_command(ctx)
        assert result.handled, result.response
        assert "Reset agent 'pip-boy'" in (result.response or "")

        # Identity + workspace-shared preserved.
        assert persona.read_text(encoding="utf-8") == "persona-body"
        assert (pip_dir / "HEARTBEAT.md").read_text(encoding="utf-8") == "hb-body"
        assert (pip_dir / "owner.md").read_text(encoding="utf-8") == "owner"
        assert (pip_dir / "bindings.json").read_text(encoding="utf-8") == "{}"
        assert (pip_dir / "agents_registry.json").is_file()
        assert (creds / "wecom.json").is_file()
        assert (archived / "dummy").is_file()

        # Memory-layer wiped. ``users/`` is re-seeded empty (mirroring
        # ``MemoryStore.__init__``) so per-user writes after the reset
        # don't trip on a missing parent directory.
        assert not (pip_dir / "memories.json").exists()
        assert not (pip_dir / "state.json").exists()
        assert users.is_dir() and list(users.iterdir()) == []
        obs_after = pip_dir / "observations"
        assert obs_after.is_dir() and list(obs_after.iterdir()) == []

    def test_reset_strips_sdk_session_entries_for_agent(
        self, tmp_path: Path,
    ):
        import json

        ctx = _build_ctx(_cli_inbound("/agent create helper"), tmp_path)
        dispatch_command(ctx)

        paths = ctx.registry.paths_for("helper")
        assert paths is not None
        sessions_path = paths.workspace_pip_dir / "sdk_sessions.json"
        sessions_path.parent.mkdir(parents=True, exist_ok=True)
        sessions_path.write_text(
            json.dumps(
                {
                    "agent:helper:cli:main": "sess-helper",
                    "agent:pip-boy:cli:main": "sess-pip-boy",
                }
            ),
            encoding="utf-8",
        )

        reset_ctx = CommandContext(
            inbound=_cli_inbound("/agent reset helper"),
            registry=ctx.registry,
            bindings=ctx.bindings,
            bindings_path=ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(reset_ctx)
        assert result.handled, result.response

        blob = json.loads(sessions_path.read_text(encoding="utf-8"))
        assert "agent:helper:cli:main" not in blob
        assert blob.get("agent:pip-boy:cli:main") == "sess-pip-boy"

    def test_reset_purges_claude_code_project_dir(
        self, tmp_path: Path, monkeypatch,
    ):
        """Reset wipes the local memory layer; CC's per-project cache
        must go with it or the very next turn rehydrates the identity
        we just reset from ``~/.claude/projects/<enc-cwd>/memory/``.
        """
        from pip_agent.memory import transcript_source

        fake_projects_root = tmp_path / "fake_claude_projects"
        monkeypatch.setattr(
            transcript_source, "DEFAULT_PROJECTS_ROOT", fake_projects_root,
        )

        ctx = _build_ctx(_cli_inbound("/agent create helper"), tmp_path)
        dispatch_command(ctx)

        paths = ctx.registry.paths_for("helper")
        assert paths is not None
        cc_dir = transcript_source.cc_project_dir_for(paths.cwd)
        (cc_dir / "memory").mkdir(parents=True)
        (cc_dir / "memory" / "MEMORY.md").write_text(
            "persisted", encoding="utf-8",
        )

        reset_ctx = CommandContext(
            inbound=_cli_inbound("/agent reset helper"),
            registry=ctx.registry,
            bindings=ctx.bindings,
            bindings_path=ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(reset_ctx)
        assert result.handled, result.response
        assert not cc_dir.exists()

    def test_reset_delegates_session_cleanup_to_host_callback(
        self, tmp_path: Path,
    ):
        """When the host is wired (``invalidate_agent`` supplied), reset
        hands session cleanup to the host instead of editing
        ``sdk_sessions.json`` directly — otherwise the host's in-memory
        ``_sessions`` map would diverge from the on-disk file.
        """
        called: list[str] = []
        ctx = _build_ctx(
            _cli_inbound("/agent create helper"),
            tmp_path,
            invalidate_agent=lambda aid: called.append(aid),
        )
        dispatch_command(ctx)

        reset_ctx = CommandContext(
            inbound=_cli_inbound("/agent reset helper"),
            registry=ctx.registry,
            bindings=ctx.bindings,
            bindings_path=ctx.bindings_path,
            memory_store=None,
            scheduler=None,
            invalidate_agent=lambda aid: called.append(aid),
        )
        result = dispatch_command(reset_ctx)
        assert result.handled, result.response
        assert called == ["helper"]


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
