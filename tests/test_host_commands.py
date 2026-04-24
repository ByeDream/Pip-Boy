"""Regression coverage for :mod:`pip_agent.host_commands`.

Phase 6 scope: the dispatcher itself — recognition rules, ACL gate,
@mention handling, and a representative handful of handler happy-paths
to prove the wiring is live. Per-handler deep coverage (every edge case
of /subagent, /admin subcommand matrix, etc.) is explicitly deferred
to Phase 11 so this file stays readable.

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
        # ``/subagnet`` is one transposition away from ``/subagent`` —
        # we want the hint surfaced so the user doesn't have to guess.
        ctx = _build_ctx(_cli_inbound("/subagnet list"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "/subagent" in (result.response or "")

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
            "/bind <id>", "/unbind",
            "/subagent", "/subagent create",
            "/subagent reset <id>",
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
# /subagent <subcommand> — dispatcher + happy paths for the sibling
# lifecycle surface. Routing (/bind, /unbind) is covered in separate
# test classes below.
# ---------------------------------------------------------------------------


class TestSubagentCommand:
    def test_no_args_lists_sub_agents(self, tmp_path: Path):
        # Bare /subagent is an alias for /subagent list. (The old
        # "show current agent detail" behavior was always echoing
        # pip-boy's own info anyway, since the family is pip-boy
        # only — /status + /memory already cover that.)
        ctx = _build_ctx(_cli_inbound("/subagent"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Agents:" in body
        assert "pip-boy" in body

    def test_list_shows_agents(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/subagent list"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "pip-boy" in body

    def test_unknown_subcommand_suggests(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/subagent lst"), tmp_path)
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
            _wecom_inbound("/subagent create helper"),
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

    def test_create_positional_label_normalises_to_lowercase_id(
        self, tmp_path: Path,
    ):
        """Mixed-case input folds to a lowercase id; display name
        defaults to the id."""
        ctx = _build_ctx(
            _cli_inbound("/subagent create HelperBot"), tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled, result.response

        cfg = ctx.registry.get_agent("helperbot")
        assert cfg is not None
        assert cfg.id == "helperbot"
        assert cfg.name == "helperbot"

    def test_create_honours_explicit_flags(self, tmp_path: Path):
        """``--id`` / ``--name`` / ``--model`` / ``--dm_scope`` all
        take precedence over the defaults derived from the positional
        label."""
        ctx = _build_ctx(
            _cli_inbound(
                '/subagent create stella --id main-helper '
                '--name "Stella Chen" --model claude-sonnet-4-5 '
                '--dm_scope main',
            ),
            tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled, result.response

        cfg = ctx.registry.get_agent("main-helper")
        assert cfg is not None
        assert cfg.id == "main-helper"
        assert cfg.name == "Stella Chen"
        assert cfg.model == "claude-sonnet-4-5"
        assert cfg.dm_scope == "main"

        # YAML frontmatter was persisted verbatim — editing it by hand
        # is the documented post-create workflow.
        paths = ctx.registry.paths_for("main-helper")
        persona = (paths.pip_dir / "persona.md").read_text(encoding="utf-8")
        assert "name: Stella Chen" in persona
        assert "model: claude-sonnet-4-5" in persona
        assert "dm_scope: main" in persona

    def test_create_inherits_root_model_when_flag_missing(
        self, tmp_path: Path,
    ):
        """Default for ``--model`` is the root agent's model, not the
        ambient ``DEFAULT_MODEL`` constant — so renaming pip-boy's
        model propagates to freshly-created sub-agents."""
        workspace = tmp_path / "workspace"
        (workspace / ".pip").mkdir(parents=True, exist_ok=True)
        # Force pip-boy's persisted model to something distinct before
        # ``_build_ctx`` loads the registry from disk.
        (workspace / ".pip" / "persona.md").write_text(
            "---\nname: Pip-Boy\nmodel: claude-haiku-4-0\n---\n"
            "# Identity\n\nYou are {agent_name}, a personal assistant.\n",
            encoding="utf-8",
        )
        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled, result.response

        cfg = ctx.registry.get_agent("helper")
        assert cfg is not None
        assert cfg.model == "claude-haiku-4-0"

    def test_create_rejects_invalid_dm_scope(self, tmp_path: Path):
        ctx = _build_ctx(
            _cli_inbound("/subagent create helper --dm_scope bogus"),
            tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "dm_scope" in (result.response or "").lower()
        assert ctx.registry.get_agent("helper") is None

    def test_create_rejects_unknown_flag(self, tmp_path: Path):
        ctx = _build_ctx(
            _cli_inbound("/subagent create helper --desc foo"), tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "unknown flag" in (result.response or "").lower()
        assert ctx.registry.get_agent("helper") is None

    def test_create_rejects_root_id(self, tmp_path: Path):
        ctx = _build_ctx(
            _cli_inbound("/subagent create pip-boy"), tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "reserved" in (result.response or "").lower()

    def test_create_id_only_no_positional(self, tmp_path: Path):
        """Fully flag-based invocation is fine too."""
        ctx = _build_ctx(
            _cli_inbound("/subagent create --id helper --name Emma"),
            tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled, result.response
        cfg = ctx.registry.get_agent("helper")
        assert cfg is not None
        assert cfg.name == "Emma"

    def test_create_inherits_pip_boy_guidance(self, tmp_path: Path):
        """New sub-agents must ship with the full operational persona
        (Identity Recognition, Tool Calling, Memory, etc.), not just
        a 4-line identity stub. Otherwise they see the injected
        ``# User`` block in their prompt but have no framework to
        interpret it — exactly the "sub-agent doesn't know the owner"
        bug from the identity-redesign thread."""
        self._seed_rich_pip_boy_persona(tmp_path)
        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled, result.response

        paths = ctx.registry.paths_for("helper")
        assert paths is not None
        persona = (paths.pip_dir / "persona.md").read_text(encoding="utf-8")

        # Identity section references Pip-Boy as parent and is written
        # as a template: ``{agent_name}`` stays literal in the file and
        # is resolved from YAML ``name:`` at prompt-compose time.
        assert "You are {agent_name}" in persona
        assert "name: helper" in persona
        assert "Pip-Boy" in persona
        assert "owner" in persona.lower()

        # Inherited sections survived the identity rewrite.
        for heading in ("# Identity Recognition", "# Tool Calling", "# Memory"):
            assert heading in persona, persona

        # Loading the config back and composing a system prompt must
        # produce the resolved ``You are helper`` — this is the real
        # contract with the model.
        prompt = ctx.registry.get_agent("helper").system_prompt(
            workdir=str(paths.cwd),
        )
        assert "You are helper," in prompt

    def test_create_identity_section_names_sub_agent_not_pip_boy(
        self, tmp_path: Path,
    ):
        """Only the Identity section is rewritten. After substitution
        the ``You are …`` line must name the new agent — not
        ``Pip-Boy``."""
        self._seed_rich_pip_boy_persona(tmp_path)
        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        dispatch_command(ctx)

        paths = ctx.registry.paths_for("helper")
        assert paths is not None

        prompt = ctx.registry.get_agent("helper").system_prompt(
            workdir=str(paths.cwd),
        )
        first_you_are = next(
            (line.strip() for line in prompt.splitlines()
             if line.strip().startswith("You are ")),
            "",
        )
        assert first_you_are.startswith("You are helper,"), first_you_are

    def test_delete_requires_yes_flag(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        dispatch_command(ctx)
        ctx2 = CommandContext(
            inbound=_cli_inbound("/subagent delete helper"),
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
            _cli_inbound("/subagent delete pip-boy --yes"), tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled
        assert "root" in (result.response or "").lower()

    def test_archive_refuses_root(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/subagent archive pip-boy"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "root" in (result.response or "").lower()

    def test_delete_invalidates_host_cache(self, tmp_path: Path):
        """Regression: after ``/subagent delete``, the host must drop
        its cached per-agent service and its session rows — otherwise
        ``flush_and_rotate`` on ``/exit`` reflects via the stale
        ``MemoryStore``, whose first ``save_state`` re-creates the
        wiped ``.pip/`` with a zombie ``state.json``.
        """
        called: list[str] = []
        create_ctx = _build_ctx(
            _cli_inbound("/subagent create helper"),
            tmp_path,
            invalidate_agent=lambda aid: called.append(aid),
        )
        dispatch_command(create_ctx)

        delete_ctx = CommandContext(
            inbound=_cli_inbound("/subagent delete helper --yes"),
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
            _cli_inbound("/subagent create helper"),
            tmp_path,
            invalidate_agent=lambda aid: called.append(aid),
        )
        dispatch_command(create_ctx)

        archive_ctx = CommandContext(
            inbound=_cli_inbound("/subagent archive helper"),
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
        """Regression: ``/subagent delete`` must also wipe CC's
        per-project cache at ``~/.claude/projects/<enc-cwd>/`` — that
        folder holds both session JSONL transcripts and CC's own
        ``memory/`` (the ``MEMORY.md`` + ``user_*.md`` cards). Without
        this cleanup, a freshly recreated agent at the same cwd
        rehydrates the previous identity's "who is my user" memory
        via CC's native recall, defeating delete.
        """
        from pip_agent.memory import transcript_source

        fake_projects_root = tmp_path / "fake_claude_projects"
        monkeypatch.setattr(
            transcript_source, "DEFAULT_PROJECTS_ROOT", fake_projects_root,
        )

        create_ctx = _build_ctx(
            _cli_inbound("/subagent create helper"), tmp_path,
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
            inbound=_cli_inbound("/subagent delete helper --yes"),
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
            _cli_inbound("/subagent create helper"), tmp_path,
        )
        dispatch_command(create_ctx)

        paths = create_ctx.registry.paths_for("helper")
        assert paths is not None
        cc_dir = transcript_source.cc_project_dir_for(paths.cwd)
        (cc_dir / "memory").mkdir(parents=True)
        (cc_dir / "memory" / "MEMORY.md").write_text("stale", encoding="utf-8")

        archive_ctx = CommandContext(
            inbound=_cli_inbound("/subagent archive helper"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(archive_ctx)
        assert result.handled, result.response
        assert not cc_dir.exists()

    def test_subagent_gated_when_on_sub_agent(self, tmp_path: Path):
        # Create a sub-agent, bind this chat to it, then verify that
        # any further /subagent call (including the bare one) bounces
        # with an /unbind redirect — sub-agents don't manage siblings.
        # Note: /bind itself is NOT gated, only /subagent is.
        create_ctx = _build_ctx(
            _cli_inbound("/subagent create helper"), tmp_path,
        )
        dispatch_command(create_ctx)
        bind_ctx = CommandContext(
            inbound=_cli_inbound("/bind helper"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        dispatch_command(bind_ctx)

        for bad in ("/subagent", "/subagent list", "/subagent create other"):
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
            assert "/unbind" in body, bad
            assert "pip-boy" in body, bad


# ---------------------------------------------------------------------------
# /bind + /unbind — symmetric routing pair, works from any agent
# ---------------------------------------------------------------------------


class TestBindCommand:
    def test_bind_no_args_lists_sub_agents(self, tmp_path: Path):
        # Usage hint should list *sub-agents*, not pip-boy itself —
        # "on pip-boy" has no binding row by construction.
        ctx = _build_ctx(_cli_inbound("/bind"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Usage:" in body
        assert "Known sub-agents:" in body
        known_line = next(
            line for line in body.splitlines()
            if line.startswith("Known sub-agents:")
        )
        assert "pip-boy" not in known_line

    def test_bind_unknown_agent_hints_create(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/bind nosuch"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "Unknown agent" in body
        assert "/subagent create" in body

    def test_bind_to_root_redirects_to_unbind(self, tmp_path: Path):
        # /bind pip-boy is not a way to "bind to root"; it would create
        # a second canonical representation of "on pip-boy" (explicit
        # binding row vs no row). Reject with a redirect to /unbind so
        # there's exactly one way to be home.
        ctx = _build_ctx(_cli_inbound("/bind pip-boy"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = (result.response or "").lower()
        assert "/unbind" in body
        assert "not supported" in body
        assert ctx.bindings.list_all() == []

    def test_bind_creates_binding_row(self, tmp_path: Path):
        # /subagent create helper → /bind helper → check the binding
        # row lands in the table and on disk.
        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        dispatch_command(ctx)

        bind_ctx = CommandContext(
            inbound=_cli_inbound("/bind helper"),
            registry=ctx.registry,
            bindings=ctx.bindings,
            bindings_path=ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(bind_ctx)
        assert result.handled
        assert "Bound to" in (result.response or "")
        bindings = bind_ctx.bindings.list_all()
        assert len(bindings) == 1
        assert bindings[0].agent_id == "helper"
        assert bind_ctx.bindings_path.is_file()

    def test_bind_works_from_sub_agent_direct_sibling_hop(
        self, tmp_path: Path,
    ):
        # This is the key asymmetry we fixed: under the old
        # /agent switch, you'd have to /home then /agent switch X to
        # hop from sub-agent A to sub-agent B. /bind works from
        # anywhere, so A → B is a single command.
        build_ctx = _build_ctx(
            _cli_inbound("/subagent create alpha"), tmp_path,
        )
        dispatch_command(build_ctx)
        dispatch_command(CommandContext(
            inbound=_cli_inbound("/subagent create beta"),
            registry=build_ctx.registry,
            bindings=build_ctx.bindings,
            bindings_path=build_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        ))

        # Bind to alpha.
        dispatch_command(CommandContext(
            inbound=_cli_inbound("/bind alpha"),
            registry=build_ctx.registry,
            bindings=build_ctx.bindings,
            bindings_path=build_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        ))
        assert [b.agent_id for b in build_ctx.bindings.list_all()] == ["alpha"]

        # Direct hop alpha → beta without going via pip-boy. Under the
        # old design this would have been rejected because /agent was
        # pip-boy-only.
        result = dispatch_command(CommandContext(
            inbound=_cli_inbound("/bind beta"),
            registry=build_ctx.registry,
            bindings=build_ctx.bindings,
            bindings_path=build_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        ))
        assert result.handled, result.response
        assert "Bound to" in (result.response or "")
        assert [b.agent_id for b in build_ctx.bindings.list_all()] == ["beta"]


class TestUnbindCommand:
    def test_unbind_on_pip_boy_is_noop(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/unbind"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = (result.response or "").lower()
        assert "already on pip-boy" in body
        assert ctx.bindings.list_all() == []

    def test_unbind_from_sub_agent_clears_binding(self, tmp_path: Path):
        create_ctx = _build_ctx(
            _cli_inbound("/subagent create helper"), tmp_path,
        )
        dispatch_command(create_ctx)
        bind_ctx = CommandContext(
            inbound=_cli_inbound("/bind helper"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        dispatch_command(bind_ctx)
        assert len(create_ctx.bindings.list_all()) == 1

        unbind_ctx = CommandContext(
            inbound=_cli_inbound("/unbind"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        )
        result = dispatch_command(unbind_ctx)
        assert result.handled
        body = (result.response or "").lower()
        assert "unbound" in body
        assert "pip-boy" in body
        assert create_ctx.bindings.list_all() == []


# ---------------------------------------------------------------------------
# /subagent reset <id> — backup · delete · rebuild · restore
# ---------------------------------------------------------------------------


class TestSubagentReset:
    def test_reset_requires_id(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/subagent reset"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Usage:" in (result.response or "")

    def test_reset_unknown_agent(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/subagent reset nosuch"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Unknown agent" in (result.response or "")

    def test_reset_sub_agent_preserves_identity_wipes_memory(
        self, tmp_path: Path,
    ):
        # Create a sub-agent and seed its .pip/ with both an identity
        # file and a memory-layer artefact, then /subagent reset.
        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
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
            inbound=_cli_inbound("/subagent reset helper"),
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

    def test_reset_refuses_root_agent(self, tmp_path: Path):
        """Root reset is rejected: the handler's own MemoryStore /
        StreamingSession are in active use, and the root ``.pip/``
        carries workspace-shared state that other agents rely on.
        The only safe way out is stopping the host and rebuilding
        offline — which the refusal message points at.
        """
        # Seed pip-boy's .pip/ with memory artefacts we want to verify
        # the refusal preserves (i.e. the handler did NOT touch them).
        ctx = _build_ctx(_cli_inbound("/subagent reset pip-boy"), tmp_path)
        paths = ctx.registry.paths_for("pip-boy")
        assert paths is not None
        pip_dir = paths.pip_dir
        pip_dir.mkdir(parents=True, exist_ok=True)
        (pip_dir / "memories.json").write_text("[\"keep\"]", encoding="utf-8")
        (pip_dir / "state.json").write_text("{\"keep\": true}", encoding="utf-8")
        (pip_dir / "owner.md").write_text("owner", encoding="utf-8")

        result = dispatch_command(ctx)
        assert result.handled, result.response
        response = result.response or ""
        assert "Cannot reset the root agent" in response
        assert "/exit" in response

        # Nothing was wiped — the refusal happens before any filesystem
        # mutation, which is the whole point.
        assert (pip_dir / "memories.json").read_text(encoding="utf-8") == "[\"keep\"]"
        assert (pip_dir / "state.json").read_text(encoding="utf-8") == "{\"keep\": true}"
        assert (pip_dir / "owner.md").read_text(encoding="utf-8") == "owner"

    def test_reset_strips_sdk_session_entries_for_agent(
        self, tmp_path: Path,
    ):
        import json

        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
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
            inbound=_cli_inbound("/subagent reset helper"),
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

        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        dispatch_command(ctx)

        paths = ctx.registry.paths_for("helper")
        assert paths is not None
        cc_dir = transcript_source.cc_project_dir_for(paths.cwd)
        (cc_dir / "memory").mkdir(parents=True)
        (cc_dir / "memory" / "MEMORY.md").write_text(
            "persisted", encoding="utf-8",
        )

        reset_ctx = CommandContext(
            inbound=_cli_inbound("/subagent reset helper"),
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
            _cli_inbound("/subagent create helper"),
            tmp_path,
            invalidate_agent=lambda aid: called.append(aid),
        )
        dispatch_command(ctx)

        reset_ctx = CommandContext(
            inbound=_cli_inbound("/subagent reset helper"),
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
