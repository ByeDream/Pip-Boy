"""Regression coverage for :mod:`pip_agent.host_commands`.

What we DO cover here:
  * A non-slash inbound is not handled.
  * An unknown slash is rejected with a helpful error (never forwarded
    to the model — typos should fail fast).
  * Leading @mention is stripped before slash detection.
  * Every command is open to every sender, with one exception: the
    CLI-only family (``/subagent`` lifecycle, ``/exit``) refuses to
    run on remote channels and is omitted from their ``/help`` listing.
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
    ensure_cli_command_markdown,
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
    dispatcher's handlers actually call (``stats``, ``load_axioms``,
    ``search``). No ACL methods — the dispatcher itself no longer
    consults the store for permissions."""

    def __init__(
        self,
        *,
        stats: dict[str, Any] | None = None,
        axioms: str = "",
        recall_hits: list[dict[str, Any]] | None = None,
    ) -> None:
        self._stats = stats or {
            "agent_id": "pip-boy",
            "observations": 0,
            "memories": 0,
            "has_axioms": False,
            "axiom_lines": 0,
        }
        self._axioms = axioms
        self._recall_hits = recall_hits or []

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
    wechat_controller: Any | None = None,
    bindings: BindingTable | None = None,
) -> CommandContext:
    # v2 layout: workspace root is the source of truth; each agent
    # has its own nested ``.pip/`` dir.
    workspace = tmp_path / "workspace"
    (workspace / ".pip").mkdir(parents=True, exist_ok=True)
    registry = AgentRegistry(workspace)
    if bindings is None:
        bindings = BindingTable()
    return CommandContext(
        inbound=inbound,
        registry=registry,
        bindings=bindings,
        bindings_path=workspace / ".pip" / "bindings.json",
        memory_store=memory_store,  # type: ignore[arg-type]
        scheduler=scheduler,
        invalidate_agent=invalidate_agent,
        wechat_controller=wechat_controller,
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

    def test_slash_t_passthrough_for_claude_code(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/T /login"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is False
        assert result.agent_user_text == "/login"
        assert result.response is None

    def test_slash_t_preserves_payload_case(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/T /Login"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is False
        assert result.agent_user_text == "/Login"

    def test_slash_t_requires_whitespace_not_tool_prefix(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/Tool"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "unknown command" in (result.response or "").lower()

    def test_slash_t_bare_is_usage(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/T"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "usage" in (result.response or "").lower()

    def test_slash_t_lowercase_is_accepted(self, tmp_path: Path):
        # Operators in shell habitually type lowercase; /t should
        # behave the same as /T so the prefix isn't a foot-gun.
        ctx = _build_ctx(_cli_inbound("/t /compact"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is False
        assert result.agent_user_text == "/compact"

    def test_slash_t_unknown_slash_warns_when_caps_known(
        self, tmp_path: Path, monkeypatch,
    ):
        # When the SDK has reported its dispatch list (via SystemMessage
        # init -> sdk_caps), typos that aren't in that list short-circuit
        # at the host with a hint instead of paying a subprocess
        # round-trip just for the SDK to reject them.
        from pip_agent import sdk_caps
        sdk_caps.reset_for_test()
        sdk_caps.record(["compact", "context", "cost", "model"])
        try:
            ctx = _build_ctx(_cli_inbound("/T /comapct"), tmp_path)
            result = dispatch_command(ctx)
            assert result.handled is True
            body = (result.response or "")
            assert "/comapct" in body
            assert "/compact" in body  # closest-match suggestion
        finally:
            sdk_caps.reset_for_test()

    def test_slash_t_known_slash_passes_through_when_caps_known(
        self, tmp_path: Path,
    ):
        from pip_agent import sdk_caps
        sdk_caps.reset_for_test()
        sdk_caps.record(["compact", "context"])
        try:
            ctx = _build_ctx(_cli_inbound("/T /Compact"), tmp_path)
            result = dispatch_command(ctx)
            assert result.handled is False
            assert result.agent_user_text == "/Compact"
        finally:
            sdk_caps.reset_for_test()

    def test_slash_t_no_warning_when_caps_unknown(self, tmp_path: Path):
        # Pre-init (caps not yet observed) we must not gate at all —
        # the SDK is the authority, the host just trusts the operator.
        from pip_agent import sdk_caps
        sdk_caps.reset_for_test()
        ctx = _build_ctx(_cli_inbound("/T /unheard-of"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is False
        assert result.agent_user_text == "/unheard-of"

    def test_slash_t_text_payload_passes_through_unchecked(
        self, tmp_path: Path,
    ):
        # ``/T`` is "raw passthrough" — non-slash payloads forward to the
        # SDK verbatim with no host-level gating, even when caps are
        # known. Slash gating only applies to ``/...`` payloads.
        from pip_agent import sdk_caps
        sdk_caps.reset_for_test()
        sdk_caps.record(["compact"])
        try:
            ctx = _build_ctx(_cli_inbound("/T hello world"), tmp_path)
            result = dispatch_command(ctx)
            assert result.handled is False
            assert result.agent_user_text == "hello world"
        finally:
            sdk_caps.reset_for_test()

    def test_typo_suggests_close_command(self, tmp_path: Path):
        # ``/subagnet`` is one transposition away from ``/subagent`` —
        # we want the hint surfaced so the user doesn't have to guess.
        ctx = _build_ctx(_cli_inbound("/subagnet list"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "/subagent" in (result.response or "")

    def test_leading_at_mention_is_stripped(self, tmp_path: Path):
        # WeCom @-prefix should not block slash detection.
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
# CLI markdown normalisation
# ---------------------------------------------------------------------------


class TestEnsureCliCommandMarkdown:
    def test_gfm_heading_passthrough(self) -> None:
        raw = "## Help\n\n- **x**\n"
        assert ensure_cli_command_markdown(raw) == raw

    def test_plain_multiline_becomes_bullets(self) -> None:
        assert ensure_cli_command_markdown("a\nb") == "- a\n- b"

    def test_single_line_unchanged(self) -> None:
        assert ensure_cli_command_markdown("one line") == "one line"

    def test_existing_markdown_list_preserved(self) -> None:
        raw = "- a\n- b\n"
        assert ensure_cli_command_markdown(raw) == raw

    def test_ordered_list_preserved(self) -> None:
        raw = "1. first\n2. second\n"
        assert ensure_cli_command_markdown(raw) == raw


# ---------------------------------------------------------------------------
# ACL gate
# ---------------------------------------------------------------------------


class TestACLGate:
    def test_remote_open_command_works_without_memory_store(
        self, tmp_path: Path,
    ):
        # Fresh boot with no MemoryStore yet — open commands still run.
        ctx = _build_ctx(_wecom_inbound("/help"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "/help" in (result.response or "")

    def test_remote_memory_command_runs(self, tmp_path: Path):
        # /memory is open on every channel now — no owner/admin gate.
        ms = _FakeMemoryStore()
        ctx = _build_ctx(_wecom_inbound("/memory"), tmp_path, memory_store=ms)
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        # Handler ran and returned stats, not a permission-denied line.
        assert "Observations:" in body

    def test_remote_subagent_is_refused(self, tmp_path: Path):
        # /subagent family is CLI-only. Remote senders get a terse
        # refusal regardless of subcommand.
        for text in (
            "/subagent",
            "/subagent list",
            "/subagent create helper",
            "/subagent delete helper --yes",
            "/subagent reset helper",
        ):
            ctx = _build_ctx(_wecom_inbound(text), tmp_path)
            result = dispatch_command(ctx)
            assert result.handled is True, text
            body = (result.response or "").lower()
            assert "cli" in body, text

    def test_remote_exit_is_refused(self, tmp_path: Path):
        ctx = _build_ctx(_wecom_inbound("/exit"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "cli" in (result.response or "").lower()


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
    def test_help_on_cli_lists_all_commands(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/help"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        for cmd in (
            "/help", "/status", "/memory", "/axioms", "/cron",
            "/mode", "/bind <id>", "/unbind",
            "/subagent", "/subagent create",
            "/subagent reset <id>",
            "/exit",
        ):
            assert cmd in body, cmd

    def test_help_renders_sdk_passthrough_section_when_unknown(
        self, tmp_path: Path,
    ):
        # ``sdk_caps`` is empty pre-init. /help must still announce the
        # SDK passthrough section so the user knows the feature exists
        # and how to populate it (otherwise they'd think /T has no
        # supported slashes at all).
        from pip_agent import sdk_caps
        sdk_caps.reset_for_test()
        try:
            ctx = _build_ctx(_cli_inbound("/help"), tmp_path)
            result = dispatch_command(ctx)
            body = result.response or ""
            assert "SDK passthrough slashes" in body
            assert "after the first agent turn" in body
        finally:
            sdk_caps.reset_for_test()

    def test_help_lists_sdk_passthrough_slashes_when_known(
        self, tmp_path: Path,
    ):
        from pip_agent import sdk_caps
        sdk_caps.reset_for_test()
        sdk_caps.record(["/compact", "/context"])
        try:
            ctx = _build_ctx(_cli_inbound("/help"), tmp_path)
            result = dispatch_command(ctx)
            body = result.response or ""
            assert "SDK passthrough slashes" in body
            assert "/compact" in body
            assert "/context" in body
        finally:
            sdk_caps.reset_for_test()

    def test_help_on_remote_hides_cli_only_commands(self, tmp_path: Path):
        # WeCom / WeChat users must not even learn that /subagent and
        # /exit exist — the CLI-only family is entirely omitted from
        # their /help so random chat peers can't probe it.
        ctx = _build_ctx(_wecom_inbound("/help"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        # Open commands still advertised.
        for cmd in ("/help", "/status", "/memory", "/bind", "/unbind"):
            assert cmd in body, cmd
        # CLI-only family hidden.
        assert "/subagent" not in body
        assert "/exit" not in body

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
        # The handler handles the missing-store case with a specific
        # message rather than crashing.
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
        body = result.response or ""
        assert "## Cron jobs" in body
        assert "No jobs configured" in body

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
        assert "## Cron jobs (2)" in body
        assert "Daily summary" in body
        assert "| yes |" in body
        assert "Flaky one" in body
        assert "| no |" in body
        assert "| 5 |" in body

    def test_exit_on_cli_returns_hint(self, tmp_path: Path):
        # CLI /exit is normally intercepted *before* dispatch, but the
        # dispatcher still owns a fallback message for any caller that
        # reaches it anyway.
        ctx = _build_ctx(_cli_inbound("/exit"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "CLI" in (result.response or "")

    def test_exit_on_non_cli_is_friendly(self, tmp_path: Path):
        # Non-CLI /exit is caught by the CLI-only dispatcher gate
        # before the handler ever runs; the refusal still mentions
        # the CLI so a confused remote operator learns where to go.
        ctx = _build_ctx(_wecom_inbound("/exit"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "CLI" in (result.response or "")


# ---------------------------------------------------------------------------
# /mode — shared agent-mode toggle
# ---------------------------------------------------------------------------


class TestModeCommand:
    def test_mode_query_reports_current_agent_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        from pip_agent.config import settings

        monkeypatch.setattr(settings, "agent_mode", "plan")

        ctx = _build_ctx(_cli_inbound("/mode"), tmp_path)
        result = dispatch_command(ctx)

        assert result.handled
        body = result.response or ""
        assert "plan" in body
        assert "default" in body

    def test_mode_sets_plan_mode_for_codex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        from pip_agent.config import settings

        monkeypatch.setattr(settings, "backend", "codex_cli")
        monkeypatch.setattr(settings, "agent_mode", "default")

        ctx = _build_ctx(_cli_inbound("/mode plan"), tmp_path)
        result = dispatch_command(ctx)

        assert result.handled
        assert settings.agent_mode == "plan"
        assert "explicit Plan Mode" in (result.response or "")

    def test_mode_sets_plan_mode_for_claude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        from pip_agent.config import settings

        monkeypatch.setattr(settings, "backend", "claude_code")
        monkeypatch.setattr(settings, "agent_mode", "default")

        ctx = _build_ctx(_cli_inbound("/mode plan"), tmp_path)
        result = dispatch_command(ctx)

        assert result.handled
        assert settings.agent_mode == "plan"
        assert "explicit Plan Mode" in (result.response or "")

    def test_mode_rejects_invalid_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        from pip_agent.config import settings

        monkeypatch.setattr(settings, "backend", "codex_cli")
        monkeypatch.setattr(settings, "agent_mode", "default")

        ctx = _build_ctx(_cli_inbound("/mode maybe"), tmp_path)
        result = dispatch_command(ctx)

        assert result.handled
        assert settings.agent_mode == "default"
        assert "Unknown" in (result.response or "")


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
        assert "## Agents" in body
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
            "Each `<user_query>` carries sender metadata; contacts are "
            "tracked in `<workspace>/.pip/addressbook/` and managed via "
            "the `remember_user` tool.\n\n"
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

    def test_create_lands_under_subagents_subdir(self, tmp_path: Path):
        """Sub-agents materialise inside the hard-coded ``workspace/``
        container, not directly under the workspace root. The user
        still types a bare id — ``/subagent create helper`` — and the
        prefix is added for them. This pins the layout so future
        refactors can't silently flatten it back."""
        from pip_agent.routing import SUBAGENTS_SUBDIR

        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled, result.response

        paths = ctx.registry.paths_for("helper")
        assert paths is not None
        workspace_root = tmp_path / "workspace"
        assert paths.cwd == workspace_root / SUBAGENTS_SUBDIR / "helper"
        assert paths.pip_dir == (
            workspace_root / SUBAGENTS_SUBDIR / "helper" / ".pip"
        )
        assert (paths.pip_dir / "persona.md").is_file()
        # The workspace root itself stays free of tenant dirs.
        assert not (workspace_root / "helper").exists()

    def test_create_honours_explicit_flags(self, tmp_path: Path):
        """``--id`` / ``--name`` / ``--model`` / ``--dm_scope`` all
        take precedence over the defaults derived from the positional
        label. ``--model`` accepts a tier name (t0/t1/t2)."""
        ctx = _build_ctx(
            _cli_inbound(
                '/subagent create stella --id main-helper '
                '--name "Stella Chen" --model t1 '
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
        assert cfg.model == "t1"
        assert cfg.dm_scope == "main"

        # YAML frontmatter was persisted verbatim — editing it by hand
        # is the documented post-create workflow.
        paths = ctx.registry.paths_for("main-helper")
        persona = (paths.pip_dir / "persona.md").read_text(encoding="utf-8")
        assert "name: Stella Chen" in persona
        assert "model: t1" in persona
        assert "dm_scope: main" in persona

    def test_create_defaults_to_tier_t0_when_flag_missing(
        self, tmp_path: Path,
    ):
        """Without ``--model``, sub-agents default to the strongest
        tier (``t0``). Concrete model names are resolved at call time
        from ``MODEL_T*`` env vars and never persisted in persona."""
        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled, result.response

        cfg = ctx.registry.get_agent("helper")
        assert cfg is not None
        assert cfg.model == "t0"

    def test_create_rejects_invalid_tier(self, tmp_path: Path):
        ctx = _build_ctx(
            _cli_inbound("/subagent create helper --model claude-opus-4-6"),
            tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled
        body = (result.response or "").lower()
        assert "model" in body
        assert "t0" in body or "tier" in body
        assert ctx.registry.get_agent("helper") is None

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

    def test_create_decouples_dirname_from_id(self, tmp_path: Path):
        """``/subagent create Foo --id alice`` puts alice's ``.pip/``
        into ``foo/`` (dirname is lowercased) while the agent's id
        remains ``alice``. This is the whole point of the
        dirname-vs-id split."""
        ctx = _build_ctx(
            _cli_inbound("/subagent create Foo --id alice --name Alice"),
            tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled, result.response

        cfg = ctx.registry.get_agent("alice")
        assert cfg is not None
        assert cfg.id == "alice"

        paths = ctx.registry.paths_for("alice")
        assert paths is not None
        # Dirname is normalized (lowercased) but distinct from id.
        assert paths.cwd.name == "foo"
        assert paths.pip_dir == paths.cwd / ".pip"
        assert (paths.pip_dir / "persona.md").is_file()

        # And the dirname-indexed lookup resolves to the same agent.
        via_dir = ctx.registry.get_by_dirname("foo")
        assert via_dir is not None
        assert via_dir.id == "alice"

        # persona.md frontmatter carries ``id:`` so the mapping
        # survives ``agents_registry.json`` being nuked.
        persona = (paths.pip_dir / "persona.md").read_text(encoding="utf-8")
        assert "id: alice" in persona

    def test_create_rejects_dirname_collision(self, tmp_path: Path):
        """Two agents can't share a directory, even with different ids."""
        ctx1 = _build_ctx(
            _cli_inbound("/subagent create Foo --id alice"), tmp_path,
        )
        assert dispatch_command(ctx1).handled

        ctx2 = _build_ctx(
            _cli_inbound("/subagent create foo --id bob"), tmp_path,
        )
        result = dispatch_command(ctx2)
        assert result.handled
        msg = (result.response or "").lower()
        assert "foo" in msg
        # Collision message should point at the offending directory or
        # existing claimant — either phrasing is acceptable.
        assert "already" in msg
        # Second agent must not have been registered.
        assert ctx2.registry.get_agent("bob") is None

    def test_create_persists_id_in_frontmatter(self, tmp_path: Path):
        """Even when dirname == id, persona.md records ``id:`` so the
        agent is self-describing if it's later moved/renamed."""
        ctx = _build_ctx(_cli_inbound("/subagent create helper"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled, result.response

        paths = ctx.registry.paths_for("helper")
        persona = (paths.pip_dir / "persona.md").read_text(encoding="utf-8")
        assert "id: helper" in persona

    def test_create_inherits_pip_boy_guidance(self, tmp_path: Path):
        """New sub-agents must ship with the full operational persona
        (Identity Recognition, Tool Calling, Memory, etc.), not just
        a 4-line identity stub. Otherwise they receive a ``user_id``
        on each ``<user_query>`` but have no framework telling them
        to call ``lookup_user`` — exactly the "sub-agent doesn't know
        who the user is" bug from the identity-redesign thread."""
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

    def test_bind_normalizes_mixed_case_input(self, tmp_path: Path):
        """Agent ids are lowercased on disk, but users may remember
        their agent by how they originally typed it. ``/bind Helper``
        must resolve to the ``helper`` directory."""
        create_ctx = _build_ctx(
            _cli_inbound("/subagent create helper"), tmp_path,
        )
        dispatch_command(create_ctx)

        result = dispatch_command(CommandContext(
            inbound=_cli_inbound("/bind Helper"),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        ))
        assert result.handled, result.response
        assert "Bound to" in (result.response or "")
        assert [b.agent_id for b in create_ctx.bindings.list_all()] == ["helper"]

    def test_bind_normalizes_quoted_multi_word_label(self, tmp_path: Path):
        """Quoted multi-word labels are parsed via shlex and folded to
        the same hyphenated id the registry created from the original
        ``/subagent create``."""
        create_ctx = _build_ctx(
            _cli_inbound('/subagent create "Project Stella"'), tmp_path,
        )
        create_result = dispatch_command(create_ctx)
        assert create_result.handled, create_result.response
        assert create_ctx.registry.get_agent("project-stella") is not None

        result = dispatch_command(CommandContext(
            inbound=_cli_inbound('/bind "Project Stella"'),
            registry=create_ctx.registry,
            bindings=create_ctx.bindings,
            bindings_path=create_ctx.bindings_path,
            memory_store=None,
            scheduler=None,
        ))
        assert result.handled, result.response
        assert [b.agent_id for b in create_ctx.bindings.list_all()] == ["project-stella"]

    def test_bind_rejects_extra_positionals(self, tmp_path: Path):
        """Two bare words are a user mistake (unquoted multi-word
        label); refuse rather than silently binding to the first
        token."""
        ctx = _build_ctx(_cli_inbound("/bind Project Stella"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        body = result.response or ""
        assert "one argument" in body.lower()
        assert ctx.bindings.list_all() == []

    def test_bind_by_dirname_when_decoupled_from_id(self, tmp_path: Path):
        """The key post-condition of the dirname/id decoupling: both
        ``/bind <id>`` and ``/bind <dirname>`` route to the same
        agent. Users don't have to remember which token was the
        "canonical" one."""
        from pip_agent.routing import BindingTable

        create_ctx = _build_ctx(
            _cli_inbound("/subagent create Foo --id alice --name Alice"),
            tmp_path,
        )
        create_result = dispatch_command(create_ctx)
        assert create_result.handled, create_result.response
        assert create_ctx.registry.get_agent("alice") is not None

        def _bind(token: str) -> tuple[BindingTable, str]:
            """Run /bind on a fresh, empty BindingTable and return
            (table, response) so each invocation is independent."""
            table = BindingTable()
            result = dispatch_command(CommandContext(
                inbound=_cli_inbound(f"/bind {token}"),
                registry=create_ctx.registry,
                bindings=table,
                bindings_path=create_ctx.bindings_path,
                memory_store=None,
                scheduler=None,
            ))
            assert result.handled, result.response
            return table, result.response or ""

        # Path 1: bind by agent_id.
        table, _ = _bind("alice")
        assert [b.agent_id for b in table.list_all()] == ["alice"]

        # Path 2: bind by dirname — same agent resolved.
        table, _ = _bind("foo")
        assert [b.agent_id for b in table.list_all()] == ["alice"]

        # Mixed-case dirname input should also resolve.
        table, _ = _bind("Foo")
        assert [b.agent_id for b in table.list_all()] == ["alice"]

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
        # Sub-agents no longer carry a local addressbook — contacts
        # live at the workspace root and are shared across agents.
        assert not (pip_dir / "addressbook").exists()
        assert not (pip_dir / "users").exists()

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
        ab = pip_dir / "addressbook"
        ab.mkdir(exist_ok=True)
        (ab / "alice.md").write_text("# Alice\n", encoding="utf-8")

        result = dispatch_command(ctx)
        assert result.handled, result.response
        response = result.response or ""
        assert "Cannot reset the root agent" in response
        assert "/exit" in response

        # Nothing was wiped — the refusal happens before any filesystem
        # mutation, which is the whole point.
        assert (pip_dir / "memories.json").read_text(encoding="utf-8") == "[\"keep\"]"
        assert (pip_dir / "state.json").read_text(encoding="utf-8") == "{\"keep\": true}"
        assert (ab / "alice.md").read_text(encoding="utf-8") == "# Alice\n"

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
# /wechat remove — accepts account_id (LHS of `/wechat list`) or agent_id
# when that agent has exactly one bound account. Multi-account agents
# refuse to guess.
# ---------------------------------------------------------------------------


class _FakeWeChatController:
    """Minimal ``WeChatController`` stub for ``/wechat`` dispatch tests."""

    def __init__(self, accounts: set[str] | None = None) -> None:
        self._accounts = set(accounts or set())
        self.removed: list[str] = []
        self.qr_calls: list[str] = []

    def list_accounts(self) -> list[dict[str, Any]]:
        return [
            {
                "account_id": aid,
                "agent_id": "",
                "logged_in": False,
            }
            for aid in sorted(self._accounts)
        ]

    def current_qr_agent(self) -> str | None:
        return None

    def remove_account(self, account_id: str) -> bool:
        if account_id in self._accounts:
            self._accounts.remove(account_id)
            self.removed.append(account_id)
            return True
        return False

    def start_qr_login(self, agent_id: str) -> tuple[bool, str]:
        self.qr_calls.append(agent_id)
        return True, f"QR scan started for agent {agent_id}"


class TestWechatRemove:
    def test_remove_by_account_id_succeeds(self, tmp_path: Path) -> None:
        from pip_agent.routing import Binding
        bindings = BindingTable()
        bindings.add(Binding(
            agent_id="test", tier=3,
            match_key="account_id", match_value="bot-a@im.bot",
        ))
        wc = _FakeWeChatController({"bot-a@im.bot"})
        ctx = _build_ctx(
            _cli_inbound("/wechat remove bot-a@im.bot"), tmp_path,
            wechat_controller=wc, bindings=bindings,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "Removed account bot-a@im.bot" in (result.response or "")
        assert wc.removed == ["bot-a@im.bot"]

    def test_remove_by_agent_id_when_unique(self, tmp_path: Path) -> None:
        from pip_agent.routing import Binding
        bindings = BindingTable()
        bindings.add(Binding(
            agent_id="test", tier=3,
            match_key="account_id", match_value="bot-a@im.bot",
        ))
        wc = _FakeWeChatController({"bot-a@im.bot"})
        ctx = _build_ctx(
            _cli_inbound("/wechat remove test"), tmp_path,
            wechat_controller=wc, bindings=bindings,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "bot-a@im.bot" in body
        assert "test" in body
        assert wc.removed == ["bot-a@im.bot"]

    def test_remove_by_agent_id_detaches_all_bound_accounts(
        self, tmp_path: Path,
    ) -> None:
        # One agent → many WeChat accounts is allowed (an account_id is
        # globally unique, but an agent can host several). ``/wechat
        # remove <agent_id>`` is the "stop using WeChat for this agent"
        # button; it detaches every bound account in one go and reports
        # the blast radius so the operator can audit it.
        from pip_agent.routing import Binding
        bindings = BindingTable()
        bindings.add(Binding(
            agent_id="dual", tier=3,
            match_key="account_id", match_value="bot-a@im.bot",
        ))
        bindings.add(Binding(
            agent_id="dual", tier=3,
            match_key="account_id", match_value="bot-b@im.bot",
        ))
        wc = _FakeWeChatController({"bot-a@im.bot", "bot-b@im.bot"})
        ctx = _build_ctx(
            _cli_inbound("/wechat remove dual"), tmp_path,
            wechat_controller=wc, bindings=bindings,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "Removed 2 accounts" in body
        assert "dual" in body
        assert "bot-a@im.bot" in body
        assert "bot-b@im.bot" in body
        assert sorted(wc.removed) == ["bot-a@im.bot", "bot-b@im.bot"]

    def test_add_lazily_bootstraps_when_controller_absent(
        self, tmp_path: Path,
    ) -> None:
        # Fresh install: no bindings, no boot-time WeChat. ``/wechat
        # add`` is the only entry point now (no more ``--wechat`` flag),
        # so it must self-bootstrap via ``ensure_wechat_controller``.
        wc = _FakeWeChatController()
        ensured: list[str] = []

        def _ensure() -> Any:
            ensured.append("called")
            return wc

        ctx = _build_ctx(
            _cli_inbound("/wechat add pip-boy"), tmp_path,
            wechat_controller=None,
        )
        ctx.ensure_wechat_controller = _ensure  # type: ignore[assignment]
        result = dispatch_command(ctx)
        assert result.handled is True
        assert ensured == ["called"]
        assert wc.qr_calls == ["pip-boy"]
        assert "QR scan started" in (result.response or "")

    def test_add_surfaces_bootstrap_failure(self, tmp_path: Path) -> None:
        def _boom() -> Any:
            raise RuntimeError("disk full")

        ctx = _build_ctx(
            _cli_inbound("/wechat add pip-boy"), tmp_path,
            wechat_controller=None,
        )
        ctx.ensure_wechat_controller = _boom  # type: ignore[assignment]
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "WeChat init failed" in body
        assert "disk full" in body

    def test_list_does_not_bootstrap(self, tmp_path: Path) -> None:
        # Only ``add`` triggers bootstrap. ``list``/``cancel``/``remove``
        # without an active controller stay as informational hints —
        # there's nothing for them to operate on yet.
        called: list[str] = []

        def _ensure() -> Any:
            called.append("nope")
            raise AssertionError("must not bootstrap on /wechat list")

        ctx = _build_ctx(
            _cli_inbound("/wechat list"), tmp_path,
            wechat_controller=None,
        )
        ctx.ensure_wechat_controller = _ensure  # type: ignore[assignment]
        result = dispatch_command(ctx)
        assert result.handled is True
        assert called == []
        assert "/wechat add" in (result.response or "")

    def test_remove_unknown_target_points_to_list(self, tmp_path: Path) -> None:
        bindings = BindingTable()
        wc = _FakeWeChatController(set())
        ctx = _build_ctx(
            _cli_inbound("/wechat remove ghost"), tmp_path,
            wechat_controller=wc, bindings=bindings,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "ghost" in body
        assert "/wechat list" in body
        assert wc.removed == []


# ---------------------------------------------------------------------------
# /plugin dispatch — wraps the bundled Claude Code CLI; we mock the
# subprocess seam (``plugins._run``) so tests never hit the real binary.
# ---------------------------------------------------------------------------


class TestPluginDispatch:

    @staticmethod
    def _patch_run(monkeypatch, results: list[tuple[str, str, int]]):
        """Replace ``plugins._run`` with a recorder.

        ``results`` queues ``(stdout, stderr, returncode)`` per call so
        a single test can simulate multi-step flows (e.g. ``search`` →
        ``list --available --json``).
        """
        from pip_agent import plugins as plug

        calls: list[dict[str, Any]] = []
        queue = list(results)

        async def _fake(*argv: str, cwd=None, timeout=None):
            calls.append({"argv": list(argv), "cwd": cwd})
            if not queue:
                return ("", "", 0)
            return queue.pop(0)

        monkeypatch.setattr(plug, "_run", _fake)
        return calls

    def test_help_prints_usage(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/plugin"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "/plugin install" in (result.response or "")

    def test_list_installed(self, tmp_path: Path, monkeypatch):
        import json as _json

        calls = self._patch_run(monkeypatch, [
            (_json.dumps([
                {"name": "web-search", "scope": "user", "description": "Web search"},
            ]), "", 0),
        ])
        ctx = _build_ctx(_cli_inbound("/plugin list"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "web-search" in body
        assert "| user |" in body
        # Argv must NOT carry --available unless explicitly requested.
        argv = calls[0]["argv"]
        assert "--available" not in argv
        assert "--json" in argv

    def test_list_available_passes_flag(self, tmp_path: Path, monkeypatch):
        calls = self._patch_run(monkeypatch, [("[]", "", 0)])
        ctx = _build_ctx(_cli_inbound("/plugin list --available"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "--available" in calls[0]["argv"]

    def test_list_available_unwraps_envelope_shape(
        self, tmp_path: Path, monkeypatch,
    ):
        # Regression guard: the bundled CLI returns
        # ``{"installed":[],"available":[…]}`` for ``--available``,
        # not a flat array. The host previously wrapped the dict in a
        # one-element list and rendered "Available plugins (1): ?".
        # We assert end-to-end that the names land in the response and
        # the count is the marketplace catalogue size.
        import json as _json

        envelope = _json.dumps({
            "installed": [],
            "available": [
                {
                    "name": "exa",
                    "description": "Exa AI web search",
                    "marketplaceName": "claude-plugins-official",
                },
                {
                    "name": "firecrawl",
                    "description": "Web scraping + LLM-ready markdown",
                    "marketplaceName": "claude-plugins-official",
                },
            ],
        })
        self._patch_run(monkeypatch, [(envelope, "", 0)])
        ctx = _build_ctx(_cli_inbound("/plugin list --available"), tmp_path)
        result = dispatch_command(ctx)
        body = result.response or ""
        assert "## Available plugins (2)" in body
        assert "exa" in body
        assert "firecrawl" in body
        assert body.count("claude-plugins-official") >= 2
        # No literal dict / question-mark fallback in the output.
        assert "': '" not in body
        assert " ?" not in body.replace(" - ", " ")

    def test_search_filters_locally(self, tmp_path: Path, monkeypatch):
        import json as _json

        self._patch_run(monkeypatch, [
            (_json.dumps([
                {"name": "pdf-tools", "description": "Read PDFs"},
                {"name": "browser", "description": "Web fetch"},
            ]), "", 0),
        ])
        ctx = _build_ctx(_cli_inbound("/plugin search pdf"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "pdf-tools" in body
        assert "browser" not in body

    def test_install_default_scope_user(self, tmp_path: Path, monkeypatch):
        calls = self._patch_run(monkeypatch, [("Installed.\n", "", 0)])
        ctx = _build_ctx(_cli_inbound("/plugin install web-search"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        argv = calls[0]["argv"]
        assert argv == [
            "plugin", "install", "web-search", "-s", "user",
        ]

    def test_install_project_scope_uses_active_agent_cwd(
        self, tmp_path: Path, monkeypatch,
    ):
        # The host MUST hand the subprocess the active agent's workdir
        # for project / local scopes; otherwise the resulting
        # ``.claude/`` lands in the host's cwd, defeating per-agent
        # isolation. ``_active_agent_cwd`` reads it from the registry,
        # which our default ``_build_ctx`` initialises against
        # ``<tmp_path>/workspace``.
        calls = self._patch_run(monkeypatch, [("ok\n", "", 0)])
        ctx = _build_ctx(
            _cli_inbound("/plugin install foo --scope project"), tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        argv = calls[0]["argv"]
        assert argv[-2:] == ["-s", "project"]
        # cwd must be a real Path under the test workspace, not None.
        assert calls[0]["cwd"] is not None
        assert "workspace" in str(calls[0]["cwd"])

    def test_install_invalid_scope_fails_fast(self, tmp_path: Path, monkeypatch):
        # Bad ``--scope`` must be rejected at the host BEFORE we spawn
        # ``claude.exe`` — otherwise the user gets a confusing CLI
        # error instead of an actionable host hint.
        called = self._patch_run(monkeypatch, [])
        ctx = _build_ctx(
            _cli_inbound("/plugin install foo --scope global"), tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        body = (result.response or "").lower()
        assert "invalid scope" in body
        assert called == []

    def test_marketplace_add_default_scope_user(
        self, tmp_path: Path, monkeypatch,
    ):
        calls = self._patch_run(monkeypatch, [("added\n", "", 0)])
        ctx = _build_ctx(
            _cli_inbound("/plugin marketplace add anthropics/claude-code"),
            tmp_path,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        argv = calls[0]["argv"]
        assert argv == [
            "plugin", "marketplace", "add",
            "anthropics/claude-code", "--scope", "user",
        ]

    def test_marketplace_list_renders_items(self, tmp_path: Path, monkeypatch):
        import json as _json

        self._patch_run(monkeypatch, [
            (_json.dumps([{"name": "official", "source": "anthropics/claude-code"}]),
             "", 0),
        ])
        ctx = _build_ctx(_cli_inbound("/plugin marketplace list"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "## Marketplaces" in body
        assert "official" in body
        assert "anthropics/claude-code" in body

    def test_subprocess_error_is_surfaced_tersely(
        self, tmp_path: Path, monkeypatch,
    ):
        self._patch_run(monkeypatch, [
            ("", "marketplace 'foo' not found", 1),
        ])
        ctx = _build_ctx(_cli_inbound("/plugin install foo"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "Plugin command failed" in body
        assert "marketplace 'foo' not found" in body

    def test_unknown_subcommand_hints_close_match(self, tmp_path: Path):
        ctx = _build_ctx(_cli_inbound("/plugin instal foo"), tmp_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        body = result.response or ""
        assert "install" in body  # hinted as did-you-mean


# ---------------------------------------------------------------------------
# CommandResult shape (sanity)
# ---------------------------------------------------------------------------


def test_command_result_defaults():
    r = CommandResult(handled=False)
    assert r.response is None
    assert r.handled is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
