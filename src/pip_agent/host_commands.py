"""Slash-command dispatch for the host layer.

Intercepts ``/cmd ...`` messages before they reach the Claude Code
subprocess so that host-layer operations (binding, memory introspection,
ACL) can be served without burning an LLM turn and without the SDK ever
seeing host-private state.

Why this lives on the host, not in CC
-------------------------------------
Anything that touches per-agent routing (``bindings.json``), the memory
store (observations / memories / axioms), or the scheduler's cron
snapshot is host state by definition — CC runs in a subprocess per
turn and has no stable identity for that state between invocations.
Routing those operations through an MCP tool would also be observable
in the JSONL transcript, which is exactly the kind of noise we spent
Phase S11 removing. So they get their own lane, short-circuited here.

Contract
--------
* Commands are **flat**. ``/subagent`` is the one family with
  subcommands (``list``, ``create``, ``archive``, ``delete``,
  ``reset``) because they manage a tightly-coupled lifecycle and
  would pollute the top-level namespace if split out.
* A handler returns a :class:`CommandResult`. ``handled=True`` stops
  further processing of the inbound; ``handled=False`` means "this
  wasn't a command I recognize, pass it on to the agent".
* **ACL gates** are owned here, not by individual handlers. The model
  is intentionally minimal:

  - Every command is open to every sender, with one exception.
  - CLI-only commands (``/subagent`` family, ``/exit``) are refused
    on remote channels (WeCom, WeChat, ...) and omitted from the
    ``/help`` listing those channels see — so a random chat peer
    doesn't even learn they exist.
  - There is no "owner" / "admin" concept. Whoever is using Pip is
    a regular contact; identity is tracked in the shared
    ``addressbook/`` via the ``remember_user`` tool.

* Unknown ``/foo`` is **handled** with a terse error so typos never
  burn an LLM turn. To forward a Claude Code built-in slash command
  (e.g. ``/compact``) through to the SDK subprocess, use ``/T /compact``
  — the ``/T`` prefix sends the payload as the **raw SDK prompt**
  (no ``<user_query>`` wrapper), which is what the SDK expects for
  ``slash_commands`` dispatch. Note that some slashes are interactive
  only (e.g. ``/login``, ``/clear``) and will not work in headless mode
  regardless of how they are sent — that is a Claude Code limitation,
  not a Pip-Boy one. See ``/help`` and the SDK ``slash_commands``
  list in the ``system/init`` message for what is dispatchable.

Out of scope (intentional omissions for v0.4.0)
-----------------------------------------------
* ``/scheduler`` / ``/lanes`` / ``/heartbeat`` / ``/trigger`` /
  ``/cron-trigger`` — surfaces for subsystems the host-rewrite
  stripped. Scheduler health is now visible via WARNING-level logs
  (coalesce misses, auto-disabled cron jobs); no chat surface needed.
* ``/profiles`` / ``/cooldowns`` / ``/stats`` / ``/simulate-failure`` /
  ``/fallback`` — the resilience runner was removed; CC owns retries.
* ``/update`` — out-of-band upgrade flow isn't re-designed yet.
* ``/clean`` — ``/subagent delete <id> --yes`` covers the narrow safe
  version (wipe metadata, keep project files); anything broader would
  be the chat-as-root-shell footgun.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pip_agent import sdk_caps
from pip_agent.channels import InboundMessage
from pip_agent.routing import (
    AgentConfig,
    AgentRegistry,
    Binding,
    BindingTable,
    build_session_key,
    normalize_agent_id,
    resolve_effective_config,
)

if TYPE_CHECKING:
    from pip_agent.host_scheduler import HostScheduler
    from pip_agent.host_state import HostState
    from pip_agent.memory import MemoryStore
    from pip_agent.tui import ThemeManager
    from pip_agent.wechat_controller import WeChatController

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CommandContext:
    """Everything a command handler might legitimately touch.

    Fields mirror ``AgentHost``'s internals by design — the dispatcher
    is essentially a view into the host with narrower write scope. Do
    NOT add new fields casually; every one here is a new thing a
    handler can mutate at will.
    """

    inbound: InboundMessage
    registry: AgentRegistry
    bindings: BindingTable
    bindings_path: Path
    memory_store: "MemoryStore | None" = None
    scheduler: "HostScheduler | None" = None
    wechat_controller: "WeChatController | None" = None
    """Host hook into the multi-account WeChat lifecycle.

    Set to ``None`` when the current run hasn't built the WeChat
    channel yet (no valid tier-3 bindings, no operator-driven QR
    login). ``/wechat add`` will lazily call
    :attr:`ensure_wechat_controller` to bootstrap on first use; the
    other ``/wechat *`` sub-commands degrade with a hint instead of
    bootstrapping silently. See
    :class:`pip_agent.wechat_controller.WeChatController` for the
    actual surface; slash handlers never touch :class:`WeChatChannel`
    directly.
    """
    ensure_wechat_controller: Callable[[], "WeChatController"] | None = None
    """Lazy bootstrap for the WeChat stack.

    ``None`` outside of the live host (e.g. unit tests) — handlers
    must check before calling. Wired by :class:`AgentHost` to its
    :meth:`ensure_wechat_controller`, which constructs + registers the
    WeChat channel and a :class:`WeChatController` against the host's
    inbound queue / stop event. Raises on bootstrap failure; the
    caller surfaces the exception as a host-level error.
    """
    invalidate_agent: Callable[[str], None] | None = None
    """Host hook to drop an agent's cached services + session rows.

    Called by lifecycle commands (``delete``, ``archive``, ``reset``)
    after the on-disk state has been mutated, so the host stops holding
    a ``MemoryStore`` that points at wiped / relocated paths. Without
    this, the cached store's next ``save_state`` (or any ``atomic_write``
    in the write path) resurrects ``.pip/`` with a stale ``state.json``
    after the agent was supposed to be gone.
    """
    theme_manager: "ThemeManager | None" = None
    """Discovery walker for builtin + ``<workspace>/.pip/themes/``.

    ``None`` outside the live host (unit tests that build
    :class:`CommandContext` directly). The ``/theme`` family bails
    with a one-line hint when this is unset, so the slash command
    never NPEs."""

    host_state: "HostState | None" = None
    """Reader/writer for ``<workspace>/.pip/host_state.json``.

    ``None`` outside the live host. ``/theme set`` writes through this
    so the selection survives restart; the boot path reads it back
    when resolving the initial theme."""

    active_theme_name: str = ""
    """Slug of the theme currently running in this host process.

    Resolved at boot from the ``host_state`` → default chain; kept in
    sync by ``set_active_theme`` whenever ``/theme set`` hot-swaps the
    live TUI."""

    tui_app: "Any | None" = None
    """Reference to the running ``PipBoyTuiApp`` when TUI is active.

    ``None`` in line mode / unit tests. ``/theme set`` uses this to
    hot-apply a new theme via ``app.call_later(app.apply_theme, bundle)``
    — the UI-thread trampoline keeps Textual happy."""

    set_active_theme: "Callable[[str], None] | None" = None
    """Host callback to update its cached ``active_theme_name``.

    Wired by :class:`AgentHost` so ``/theme set`` can keep the host's
    own view consistent with whatever it just applied to the TUI. Not
    wired in line mode (the field is for the CLI's runtime identity)."""


@dataclass(slots=True)
class CommandResult:
    handled: bool
    response: str | None = None
    agent_user_text: str | None = None


_MD_ORDERED_ITEM = re.compile(r"^\d+\.\s")


def ensure_cli_command_markdown(text: str) -> str:
    """Prepare slash-command output for :func:`pip_agent.host_io.emit_agent_markdown`.

    Hand-authored GFM (``/help`` — leading ``#`` or fenced code) is
    returned unchanged. Plain multi-line listings become one Markdown
    bullet per line so newlines are not collapsed into a single
    paragraph.
    """
    if not text:
        return text
    lead = text.lstrip()
    if lead.startswith("#") or lead.startswith("```"):
        return text
    core = text.strip("\n")
    if "\n" not in core:
        return text
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        s = line.strip()
        if not s:
            out.append("")
            continue
        if s.startswith(("- ", "* ", "> ")):
            out.append(line)
            continue
        if s.startswith(">"):
            out.append(line)
            continue
        if _MD_ORDERED_ITEM.match(s):
            out.append(line)
            continue
        out.append(f"- {s}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------


# ``@alice /help`` — WeCom / WeChat mention prefixes. Strip so the
# slash detection below doesn't miss the command. Only leading mentions
# are stripped; a ``@user`` mid-argument is passed through unchanged.
_AT_MENTION_RE = re.compile(r"^(?:@\S*\s+)+")


# ``/T <payload>`` — explicit SDK passthrough. Case-insensitive to match
# the mixed-case habits of operators in shells; the **payload** still
# preserves its original case (some slashes have arguments where case
# matters). Whitespace between ``/T`` and ``<payload>`` is mandatory so
# unrelated commands like ``/Tool`` / ``/team`` (none today, but keep
# the namespace open) are not swallowed by this rule.
_T_BARE_RE = re.compile(r"(?i)^/T\s*$")
_T_PREFIX_RE = re.compile(r"(?i)^/T\s+(.*)$", re.DOTALL)


_T_USAGE = (
    "Usage: `/T <text>` — forwards <text> to the SDK as a raw prompt "
    "(no `<user_query>` wrapper). Use it to dispatch Claude Code slash "
    "commands such as `/T /compact` or `/T /context`. Some slashes "
    "(e.g. `/login`, `/clear`) only work in the interactive Claude "
    "Code CLI and will not run via SDK regardless. `/T` must be "
    "followed by whitespace before the payload."
)


def _suggest_command(cmd: str) -> str | None:
    """Return the closest registered command to ``cmd`` (or ``None``).

    Used to turn typos like ``/swicth`` into actionable ``Did you mean
    /switch?`` hints. We keep the threshold tight (0.7 ratio) so unrelated
    garbage doesn't get a false suggestion.
    """
    from difflib import get_close_matches

    matches = get_close_matches(cmd, _HANDLERS.keys(), n=1, cutoff=0.7)
    return matches[0] if matches else None


def dispatch_command(ctx: CommandContext) -> CommandResult:
    """Try to intercept the inbound as a slash command.

    Returns ``CommandResult(handled=False)`` if the text should become an
    agent/SDK turn — either unrecognized as a host command (after
    stripping ``/T`` passthrough) or an explicit ``/T <payload>`` with
    ``agent_user_text`` set for the caller to substitute.
    """
    raw = ctx.inbound.text
    if not isinstance(raw, str):
        return CommandResult(handled=False)

    text = _AT_MENTION_RE.sub("", raw.strip()).strip()
    if not text.startswith("/"):
        return CommandResult(handled=False)

    if _T_BARE_RE.match(text):
        return CommandResult(handled=True, response=_T_USAGE)

    m_pt = _T_PREFIX_RE.match(text)
    if m_pt:
        payload = m_pt.group(1).strip()
        if not payload:
            return CommandResult(handled=True, response=_T_USAGE)
        # If the payload looks like a slash command and we have already
        # observed the SDK's dispatchable list (via ``SystemMessage(init)``
        # → :mod:`pip_agent.sdk_caps`), gate typos here so the user sees
        # an immediate hint instead of paying a subprocess round-trip
        # only for the SDK to reply ``"/foo isn't available in this
        # environment."``. Non-slash payloads pass through verbatim —
        # ``/T`` is "raw passthrough", no further rules.
        if payload.startswith("/"):
            first_token = payload.split(None, 1)[0]
            slash_name = first_token.lstrip("/").strip().lower()
            caps = sdk_caps.get()
            if caps is not None and slash_name and slash_name not in caps:
                hint = ""
                if caps:
                    from difflib import get_close_matches
                    matches = get_close_matches(
                        slash_name, sorted(caps), n=1, cutoff=0.6,
                    )
                    if matches:
                        hint = f" Did you mean `/{matches[0]}`?"
                return CommandResult(
                    handled=True,
                    response=(
                        f"`{first_token}` is not in this SDK session's "
                        f"slash list.{hint} Run `/help` to see what is "
                        "dispatchable."
                    ),
                )
        return CommandResult(handled=False, agent_user_text=payload)

    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    handler = _HANDLERS.get(cmd)
    if handler is None:
        # Strict parsing: any ``/...`` token that isn't a known command
        # fails fast instead of being forwarded to the model. This avoids
        # typos (``/swicth``) silently burning an LLM turn and guarantees
        # that slash-prefixed text is always resolved by the host.
        suggestion = _suggest_command(cmd)
        hint = f" Did you mean `{suggestion}`?" if suggestion else ""
        return CommandResult(
            handled=True,
            response=(
                f"Unknown command `{cmd}`.{hint} "
                "Type `/help` for the full list."
            ),
        )

    # --- ACL gate ---
    #
    # Only one rule: CLI-only commands (destructive / operator-flavoured
    # things like the ``/subagent`` lifecycle family and ``/exit``) are
    # refused on remote channels. Everything else is open.
    if cmd in _CLI_ONLY_COMMANDS and ctx.inbound.channel != "cli":
        return CommandResult(
            handled=True,
            response=f"`{cmd}` is only available from the CLI.",
        )

    try:
        return handler(ctx, args)
    except Exception as exc:  # noqa: BLE001
        # Command handlers must never take the host down. Log with
        # traceback for debugging but surface a terse user message.
        log.exception("Slash command %s crashed", cmd)
        return CommandResult(handled=True, response=f"[error] {exc}")


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


_HELP_COMMON = (
    "## Available commands\n\n"
    "### Session & memory\n\n"
    "- **`/help`** — Show this help.\n"
    "- **`/status`** — Current agent, session, and binding.\n"
    "- **`/memory`** — Memory statistics for the current agent.\n"
    "- **`/axioms`** — Current judgment principles.\n"
    "- **`/recall <query>`** — Search stored memories.\n"
    "- **`/cron`** — List scheduled cron jobs.\n\n"
    "### Routing\n\n"
    "- **`/bind <id>`** — Route this chat to sub-agent `<id>`. In a "
    "group chat creates a guild-level binding; in a private chat, a "
    "peer-level binding. Persisted to "
    "`<workspace>/.pip/bindings.json`. `/bind pip-boy` redirects to "
    "`/unbind`.\n"
    "- **`/unbind`** — Clear this chat's binding; routing falls back "
    "to pip-boy (no-op if already on pip-boy).\n\n"
    "### SDK passthrough\n\n"
    "- **`/T <text>`** — Forward `<text>` to the SDK as a raw Claude Code "
    "prompt (e.g. `/T /compact`). Typos like `/T /hlp` stay host-side errors "
    "(never LLM turns). Some SDK slashes are interactive-only and will not "
    "work in SDK mode (`/login`, `/clear`). Passthrough slashes are listed "
    "at the end of `/help` after the first agent turn.\n\n"
    "### Themes\n\n"
    "- **`/theme list`** — Installed TUI themes (active marked with `*`).\n"
    "- **`/theme set <name>`** — Switch theme immediately and persist.\n"
    "- **`/theme refresh`** — Rescan `.pip/themes/` for newly added themes.\n\n"
    "### Plugins\n\n"
    "- **`/plugin help`** — Full `/plugin` usage "
    "(install, marketplace, scopes).\n"
    "- **`/plugin list [--available]`** — Installed plugins, or "
    "marketplace catalog.\n"
    "- **`/plugin search <query>`** — Search marketplace by name / tag / "
    "description.\n"
    "- **`/plugin install <spec> [--scope SCOPE]`** — Install "
    "(default `scope=user`; `project` or `local` for per-agent).\n"
    "- **`/plugin marketplace list`** / "
    "**`/plugin marketplace add <src> [--scope SCOPE]`** — List or "
    "register a marketplace (owner/repo, `https` git URL, or path). "
    "Other verbs: enable, disable, uninstall, remove, update."
)


_HELP_CLI_EXTRA = (
    "\n\n## CLI-only (refused on remote channels)\n\n"
    "- **`/exit`** — Quit Pip-Boy (reflect / rotate run on the way out).\n"
    "- **`/wechat list`** — Registered WeChat accounts and bindings.\n"
    "- **`/wechat add <agent_id>`** — QR login; bind a new account to "
    "`<agent_id>`.\n"
    "- **`/wechat cancel`** — Abort in-progress QR login.\n"
    "- **`/wechat remove <id>`** — Stop polling and delete credential + "
    "binding. `<id>` is either an `account_id` from `/wechat list`, or an "
    "`agent_id` to detach every WeChat account bound to that agent.\n"
    "- **`/subagent`** — List sub-agents (pip-boy host only).\n"
    "- **`/subagent create <label> [--id ID] [--name NAME] [--model t0|t1|t2] "
    "[--dm_scope SCOPE]`** — Create a sub-agent. Defaults: id from "
    "normalized label; `name=id`; model tier `t0`; `dm_scope=per-guild`. "
    "Valid `dm_scope`: `main`, `per-guild`, `per-guild-peer`.\n"
    "- **`/subagent archive <id>`** — Move `<id>/.pip/` under "
    "`.pip/archived/` (project files untouched).\n"
    "- **`/subagent delete <id> --yes`** — Wipe `<id>/.pip/` "
    "(project files untouched).\n"
    "- **`/subagent reset <id>`** — Rebuild `<id>/.pip/` from minimal "
    "backup; keeps `persona.md` and `HEARTBEAT.md`. Not allowed on the "
    "pip-boy root agent."
)


def _cmd_help(ctx: CommandContext, _args: str) -> CommandResult:
    text = _HELP_COMMON
    if ctx.inbound.channel == "cli":
        text = text + "\n" + _HELP_CLI_EXTRA
    # Surface what ``/T <slash>`` will actually dispatch in the current
    # SDK session. Cached lazily on the first ``SystemMessage(init)``
    # (see :mod:`pip_agent.sdk_caps`); absent before any agent turn has
    # run, so we render a placeholder explaining the lazy-fill instead
    # of silently omitting the section.
    caps = sdk_caps.get()
    if caps:
        slashes = ", ".join(f"`/{n}`" for n in sorted(caps))
        body = slashes
    else:
        body = (
            "*(Populated after the first agent turn — send any message to "
            "this agent first, then re-run `/help`.)*"
        )
    text = (
        f"{text}\n\n## SDK passthrough slashes (use with `/T`)\n\n{body}"
    )
    return CommandResult(handled=True, response=text)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


def _cmd_status(ctx: CommandContext, _args: str) -> CommandResult:
    inbound = ctx.inbound
    agent_id, binding = ctx.bindings.resolve(
        channel=inbound.channel,
        account_id=inbound.account_id,
        guild_id=inbound.guild_id,
        peer_id=inbound.peer_id,
    )
    if not agent_id:
        agent_id = ctx.registry.default_agent().id
        binding = None

    agent = ctx.registry.get_agent(agent_id) or ctx.registry.default_agent()
    effective = resolve_effective_config(agent, binding)
    sk = build_session_key(
        agent_id=effective.id,
        channel=inbound.channel,
        peer_id=inbound.peer_id,
        guild_id=inbound.guild_id,
        is_group=inbound.is_group,
        dm_scope=effective.effective_dm_scope,
    )

    from pip_agent.models import primary_model

    tier = effective.tier
    resolved = primary_model(tier)  # type: ignore[arg-type]
    model_display = f"{tier} ({resolved})" if resolved else f"{tier} (no model configured)"
    lines = [
        f"Agent: {agent.name or agent.id} ({agent.id})",
        f"Model: {model_display}",
        f"Scope: {effective.effective_dm_scope}",
        f"Session: {sk}",
        f"Channel: {inbound.channel}",
        f"Binding: {binding.display() if binding else '(none — default)'}",
    ]
    if inbound.is_group:
        lines.append(f"Guild: {inbound.guild_id}")
    lines.append(f"Peer: {inbound.peer_id}")
    return CommandResult(handled=True, response="\n".join(lines))


# ---------------------------------------------------------------------------
# /memory, /axioms, /recall
# ---------------------------------------------------------------------------


def _cmd_memory(ctx: CommandContext, _args: str) -> CommandResult:
    if ctx.memory_store is None:
        return CommandResult(handled=True, response="Memory system not initialized.")
    s = ctx.memory_store.stats()
    lines = [
        f"Agent: {s['agent_id']}",
        f"Observations: {s['observations']}",
        f"Memories: {s['memories']}",
        f"Axioms: {'yes' if s['has_axioms'] else 'none'} ({s['axiom_lines']} lines)",
    ]
    for key, label in (
        ("last_reflect_at", "Last reflect"),
        ("last_consolidate_at", "Last consolidate"),
    ):
        ts = s.get(key)
        if ts:
            t = datetime.fromtimestamp(float(ts), tz=UTC)
            lines.append(f"{label}: {t.strftime('%Y-%m-%d %H:%M UTC')}")
    return CommandResult(handled=True, response="\n".join(lines))


def _cmd_axioms(ctx: CommandContext, _args: str) -> CommandResult:
    if ctx.memory_store is None:
        return CommandResult(handled=True, response="Memory system not initialized.")
    axioms = ctx.memory_store.load_axioms()
    if not axioms:
        return CommandResult(
            handled=True,
            response="No axioms yet. They emerge after enough conversations.",
        )
    return CommandResult(handled=True, response=axioms)


def _cmd_recall(ctx: CommandContext, args: str) -> CommandResult:
    if ctx.memory_store is None:
        return CommandResult(handled=True, response="Memory system not initialized.")
    query = args.strip()
    if not query:
        return CommandResult(handled=True, response="Usage: /recall <query>")
    results = ctx.memory_store.search(query, top_k=5)
    if not results:
        return CommandResult(handled=True, response="(no matching memories)")
    lines = [
        f"- {r.get('text', '')} (score: {r.get('score', 0)})"
        for r in results
    ]
    return CommandResult(handled=True, response="\n".join(lines))


# ---------------------------------------------------------------------------
# /cron — read-only surface; CRUD is done via MCP tools so the LLM can
# drive it
# ---------------------------------------------------------------------------


def _cmd_cron(ctx: CommandContext, _args: str) -> CommandResult:
    sched = ctx.scheduler
    if sched is None:
        return CommandResult(handled=True, response="Scheduler not running.")
    jobs = sched.list_jobs()
    if not jobs:
        return CommandResult(
            handled=True,
            response="## Cron jobs\n\n*(No jobs configured.)*",
        )

    lines = [
        f"## Cron jobs ({len(jobs)})",
        "",
        "| On | Name | Schedule | Next run (UTC) | Errors |",
        "| --- | --- | --- | --- | --- |",
    ]
    for j in jobs:
        on = "yes" if j.get("enabled", True) else "no"
        errors = j.get("consecutive_errors", 0)
        kind = j.get("schedule_kind", "?")
        name = j.get("name") or j.get("id") or "?"
        next_at = j.get("next_fire_at")
        if next_at:
            t = datetime.fromtimestamp(float(next_at), tz=UTC)
            next_str = t.strftime("%Y-%m-%d %H:%M")
        else:
            next_str = "n/a"
        lines.append(
            f"| {_md_table_cell(on)} | {_md_table_cell(name)} | "
            f"{_md_table_cell(str(kind))} | {_md_table_cell(next_str)} | "
            f"{_md_table_cell(str(errors))} |"
        )
    return CommandResult(handled=True, response="\n".join(lines))


# ---------------------------------------------------------------------------
# /subagent — pip-boy's management console for sibling sub-agents
# /bind + /unbind — symmetric routing pair for the current chat
# ---------------------------------------------------------------------------
#
# Naming
# ------
# ``/subagent`` (not ``/agent``) because the verb surface ONLY manages
# siblings under pip-boy. "/agent" misleadingly suggested "the current
# agent's console"; "/subagent" matches the actual scope.
#
# Routing is a separate, symmetric pair:
#
#   /bind <id>   — route this chat to sub-agent <id>
#   /unbind      — clear the binding, fall back to pip-boy
#
# These are **not** nested under /subagent, because they're navigation
# actions on *this chat*, not management of the sibling registry. They
# work from any agent (including from one sub-agent to another),
# unlike the lifecycle verbs below.
#
# Design principles (agreed in the identity-redesign thread):
#   * ``/subagent`` is **pip-boy exclusive**: create/archive/delete/reset
#     of siblings is only accessible when the current chat is bound to
#     pip-boy. Sub-agents focus on their own work; they don't manage
#     siblings. To go back to pip-boy, use ``/unbind``.
#   * ``/bind`` / ``/unbind`` are **not** gated to pip-boy. They mutate
#     this chat's routing only, which is a user navigation concern.
#   * Subcommand style (``git``-like) for /subagent, NOT ``--flag`` style.
#   * Exactly one verb per action. No duplication between /bind and
#     /subagent.
#   * Zero CLI options beyond the subcommand + id. Per-agent tweaks
#     (model, dm_scope, description, binding scope) live in
#     ``persona.md`` / ``agents_registry.json`` / ``bindings.json`` —
#     edit the file if you want to deviate.
#   * archive/delete operate on the agent *identity surface* only
#     (``.pip/``); project files in the sub-agent's cwd are never
#     touched (see :meth:`AgentRegistry.remove_agent`).
#   * ``/subagent reset <id>`` preserves identity (``persona.md`` +
#     ``HEARTBEAT.md``); everything else in the agent's ``.pip/``
#     is wiped and left to be lazily re-created. Root (pip-boy) is
#     refused — see ``_agent_reset`` for the self-surgery argument.


def _persist_agent_md(cfg: AgentConfig, pip_dir: Path | None) -> None:
    """Write an AgentConfig to ``<pip_dir>/persona.md``.

    The ``id:`` field is always written so persona.md is self-describing
    — if the directory is renamed on disk later, the agent still knows
    its own identity. ``agent_config_from_file`` reads this field and
    falls back to the directory name only when the frontmatter is
    silent, which keeps legacy persona.md files loading.
    """
    if not pip_dir:
        return
    pip_dir.mkdir(parents=True, exist_ok=True)
    md_path = pip_dir / "persona.md"

    lines = ["---", f"id: {cfg.id}", f"name: {cfg.name}"]
    if cfg.model:
        lines.append(f"model: {cfg.model}")
    if cfg.dm_scope:
        lines.append(f"dm_scope: {cfg.dm_scope}")
    lines.append("---\n")
    frontmatter = "\n".join(lines)
    body = cfg.system_body or ""
    md_path.write_text(frontmatter + body + "\n", encoding="utf-8")


def _resolved_agent_id(ctx: CommandContext) -> str:
    """Return the agent id currently routed for this inbound."""
    inbound = ctx.inbound
    aid, _ = ctx.bindings.resolve(
        channel=inbound.channel,
        account_id=inbound.account_id,
        guild_id=inbound.guild_id,
        peer_id=inbound.peer_id,
    )
    return aid or ctx.registry.default_agent().id


def _purge_bindings_for(ctx: CommandContext, agent_id: str) -> None:
    """Drop every binding that routes to ``agent_id`` and persist."""
    removed = False
    for b in list(ctx.bindings.list_all()):
        if b.agent_id == agent_id:
            ctx.bindings.remove(b.match_key, b.match_value)
            removed = True
    if removed:
        try:
            ctx.bindings.save(ctx.bindings_path)
        except Exception:
            log.exception("Failed to persist bindings after purge")


def _purge_cc_project_dir(cwd: Path) -> Path | None:
    """Delete Claude Code's project directory for ``cwd`` if present.

    CC keeps per-project state under ``~/.claude/projects/<enc-cwd>/``:
    session JSONL transcripts *and* its native ``memory/`` folder
    (``MEMORY.md`` + ``user_*.md`` cards). That folder survives
    ``/subagent delete`` by default, so a freshly recreated agent at the
    same cwd inherits the previous identity's "who is my user" memory
    via CC's own recall — defeating the purpose of the delete.

    Returning the path (or ``None`` if nothing was there) lets callers
    surface the cleanup in their response so the operator can see what
    was touched outside ``<workspace>/``.
    """
    import shutil

    from pip_agent.memory.transcript_source import cc_project_dir_for

    project_dir = cc_project_dir_for(cwd)
    if not project_dir.is_dir():
        return None
    try:
        shutil.rmtree(project_dir)
    except OSError:
        log.exception("Failed to purge CC project dir %s", project_dir)
        return None
    return project_dir


def _cmd_subagent(ctx: CommandContext, args: str) -> CommandResult:
    """Dispatcher for the ``/subagent`` subcommand family — pip-boy only.

    Subcommands:

    * ``/subagent``                           — list all known sub-agents
                                                  (alias for ``/subagent list``)
    * ``/subagent list``                      — list all known sub-agents
    * ``/subagent create <label> [flags]``    — materialise
                                                  ``<workspace>/<id>/.pip/``.
                                                  Flags: ``--id``, ``--name``,
                                                  ``--model``, ``--dm_scope``.
    * ``/subagent archive <id>``              — move ``<id>/.pip/`` to
                                                  ``.pip/archived/``
    * ``/subagent delete <id> --yes``         — rmtree ``<id>/.pip/`` (project
                                                  files kept)
    * ``/subagent reset <id>``                — factory-reset ``<id>``'s memory
                                                  (identity preserved; see
                                                  helper below)

    Routing (/bind, /unbind) is deliberately NOT a subcommand here:
    it's user navigation, not sibling management, and it works from
    any agent. See :func:`_cmd_bind` / :func:`_cmd_unbind`.

    Pip-boy gating
    --------------
    The whole family is **only usable when the current chat is bound
    to pip-boy**. From a sub-agent, ``/subagent`` returns a polite
    redirect to ``/unbind`` — sub-agents don't manage siblings.

    Channel gating
    --------------
    The whole family is CLI-only — the top-level dispatcher rejects
    it on remote channels before we get here. No further per-subcommand
    gate is needed.
    """
    try:
        tokens = shlex.split(args) if args.strip() else []
    except ValueError as exc:
        return CommandResult(handled=True, response=f"Parse error: {exc}")

    current_id = _resolved_agent_id(ctx)
    root_id = ctx.registry.default_agent().id
    if current_id != root_id:
        return CommandResult(
            handled=True,
            response=(
                f"`/subagent` is only available from {root_id}. "
                f"You are currently on `{current_id}`. "
                "Run `/unbind` to return to pip-boy first."
            ),
        )

    # Bare ``/subagent`` is an alias for ``/subagent list`` — the old
    # "show current agent" branch was always dumping pip-boy's detail
    # (because the family is gated to pip-boy anyway), which made it
    # a weird echo of ``/status`` + ``/memory``. Listing siblings is
    # the genuinely useful zero-arg form.
    if not tokens:
        return _agent_list(ctx, [])

    sub = tokens[0].lower()
    tail = tokens[1:]
    handler = _SUBAGENT_SUBCOMMANDS.get(sub)
    if handler is None:
        from difflib import get_close_matches
        hint = get_close_matches(sub, _SUBAGENT_SUBCOMMANDS.keys(), n=1, cutoff=0.6)
        suffix = f" Did you mean `/subagent {hint[0]}`?" if hint else ""
        return CommandResult(
            handled=True,
            response=(
                f"Unknown /subagent subcommand '{sub}'.{suffix}\n"
                "Valid: list, create, archive, delete, reset. "
                "Run `/help` for full usage."
            ),
        )

    return handler(ctx, tail)


def _agent_list(ctx: CommandContext, _tail: list[str]) -> CommandResult:
    agents = ctx.registry.list_agents()
    if not agents:
        return CommandResult(
            handled=True,
            response="## Agents\n\n*(No agents registered.)*",
        )

    bound_id = _resolved_agent_id(ctx)
    default_id = ctx.registry.default_agent().id

    lines = [
        f"## Agents ({len(agents)})",
        "",
        "| Kind | Id | Display name | Routed here | Description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for cfg in sorted(agents, key=lambda a: (a.id != default_id, a.id)):
        meta = ctx.registry.metadata_for(cfg.id)
        kind = str(meta.get("kind", "sub"))
        here = "yes" if cfg.id == bound_id else ""
        desc = str(meta.get("description", "") or "")
        disp = cfg.name or cfg.id
        lines.append(
            f"| {_md_table_cell(kind)} | {_md_table_cell(cfg.id)} | "
            f"{_md_table_cell(str(disp))} | {_md_table_cell(here)} | "
            f"{_md_table_cell(desc, limit=64)} |"
        )
    lines.append("")
    lines.append(
        "> **Routed here** = this chat is bound to that agent. "
        "Use `/bind <id>` / `/unbind` to switch.",
    )
    return CommandResult(handled=True, response="\n".join(lines))


_VALID_DM_SCOPES = {"main", "per-guild", "per-guild-peer"}

_CREATE_USAGE = (
    "Usage: /subagent create <label> [--id ID] [--name NAME] "
    "[--model {t0|t1|t2}] [--dm_scope SCOPE]\n"
    "The positional <label> is the directory name under the workspace "
    "root. --id is the agent's identity key (registry + session + bind "
    "target); it defaults to <label> when omitted, so the two stay in "
    "sync for the simple case. Provide --id to decouple them.\n"
    "Defaults: --name <id>, --model t0, --dm_scope per-guild.\n"
    "--model picks a tier (t0 strongest, t2 cheapest); concrete model "
    "names live in MODEL_T0/MODEL_T1/MODEL_T2 in .env.\n"
    "Valid scopes: main | per-guild | per-guild-peer."
)


def _parse_create_flags(tokens: list[str]) -> tuple[dict[str, str], str | None]:
    """Parse ``[positional] [--flag value]...`` into ``(opts, error)``.

    Recognised flags: ``--id``, ``--name``, ``--model``, ``--dm_scope``.
    ``--dm-scope`` is accepted as an alias so either spelling works.
    At most one positional argument (the label, used as directory name)
    is allowed.
    """
    allowed = {"--id", "--name", "--model", "--dm_scope", "--dm-scope"}
    opts: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            if tok not in allowed:
                return {}, f"Unknown flag '{tok}'."
            if i + 1 >= len(tokens):
                return {}, f"Flag '{tok}' needs a value."
            key = "--dm_scope" if tok == "--dm-scope" else tok
            opts[key] = tokens[i + 1]
            i += 2
            continue
        positional.append(tok)
        i += 1

    if len(positional) > 1:
        return {}, "Only one positional label is allowed."
    if positional:
        opts["__label__"] = positional[0]
    return opts, None


def _agent_create(ctx: CommandContext, tail: list[str]) -> CommandResult:
    """``/subagent create [label] [--id …] [--name …] [--model …] [--dm_scope …]``.

    The positional ``<label>`` becomes the **directory name** under the
    workspace root. ``--id`` sets the **agent id** (registry key / bind
    target / session key). When ``--id`` is omitted, id defaults to the
    dirname so ``/subagent create helper`` gives you a tidy
    ``helper/.pip/`` + id ``helper``. Pass ``--id`` when you want them
    decoupled — e.g. ``/subagent create Foo --id alice`` puts alice's
    ``.pip/`` inside ``foo/`` (dirnames are lowercased). After that
    both ``/bind foo`` and ``/bind alice`` route to the same agent.
    """
    opts, err = _parse_create_flags(tail)
    if err is not None:
        return CommandResult(handled=True, response=f"{err}\n{_CREATE_USAGE}")
    if not opts:
        return CommandResult(handled=True, response=_CREATE_USAGE)

    label = opts.get("__label__", "")
    raw_id = opts.get("--id")
    if not label.strip() and not (raw_id and raw_id.strip()):
        return CommandResult(
            handled=True,
            response=(
                "Cannot create agent — provide a positional label "
                f"(used as dirname) and/or --id.\n{_CREATE_USAGE}"
            ),
        )

    dirname = normalize_agent_id(label) if label.strip() else normalize_agent_id(raw_id or "")
    agent_id = normalize_agent_id(raw_id) if raw_id and raw_id.strip() else dirname

    default_id = ctx.registry.default_agent().id
    if agent_id == default_id or dirname == default_id:
        return CommandResult(
            handled=True,
            response=(
                f"Cannot use '{default_id}': reserved for the root agent."
            ),
        )
    if ctx.registry.get_agent(agent_id) is not None:
        return CommandResult(
            handled=True, response=f"Agent id '{agent_id}' already exists.",
        )
    # Dirname uniqueness: two agents can't share a directory on disk.
    if ctx.registry.get_by_dirname(dirname) not in (None, ctx.registry.default_agent()):
        existing = ctx.registry.get_by_dirname(dirname)
        return CommandResult(
            handled=True,
            response=(
                f"Directory '{dirname}/' is already claimed by agent "
                f"'{existing.id}'. Pick a different label or archive the "
                "existing agent first."
            ),
        )
    # Also refuse if the directory exists on disk with a .pip/ we
    # haven't registered — that's a collision we can't silently
    # overwrite.
    if ctx.registry.workspace_root is not None:
        candidate = ctx.registry.workspace_root / dirname / ".pip"
        if candidate.exists():
            return CommandResult(
                handled=True,
                response=(
                    f"Directory '{dirname}/.pip' already exists on disk "
                    "but isn't registered. Remove it manually or pick a "
                    "different label."
                ),
            )

    display_name = opts.get("--name") or agent_id

    # ``model`` is a tier name (t0/t1/t2) — concrete model identifiers
    # live in MODEL_T* in .env. Default to t0 so a fresh sub-agent runs
    # on the strongest tier unless the operator explicitly downshifts.
    from pip_agent.models import DEFAULT_TIER, VALID_TIERS

    raw_model = (opts.get("--model") or "").strip().lower()
    if raw_model and raw_model not in VALID_TIERS:
        return CommandResult(
            handled=True,
            response=(
                f"Invalid --model '{raw_model}'. "
                f"Valid: {', '.join(sorted(VALID_TIERS))}."
            ),
        )
    model = raw_model or DEFAULT_TIER

    dm_scope = opts.get("--dm_scope") or "per-guild"
    if dm_scope not in _VALID_DM_SCOPES:
        return CommandResult(
            handled=True,
            response=(
                f"Invalid --dm_scope '{dm_scope}'. "
                f"Valid: {', '.join(sorted(_VALID_DM_SCOPES))}."
            ),
        )

    cfg, err = _create_agent_on_disk(
        ctx.registry,
        agent_id,
        dirname=dirname,
        name=display_name,
        model=model,
        dm_scope=dm_scope,
    )
    if err or cfg is None:
        return CommandResult(
            handled=True,
            response=err or f"Failed to create agent '{agent_id}'.",
        )
    paths = ctx.registry.paths_for(cfg.id)
    loc = f" at {paths.cwd}" if paths is not None else ""
    detail = f"  id={agent_id}  dir={dirname}/  name={display_name}"
    if model:
        detail += f"  model={model}"
    detail += f"  dm_scope={dm_scope}"
    bind_hint = (
        f"Use `/bind {agent_id}` (or `/bind {dirname}`) to route this chat to it."
        if agent_id != dirname
        else f"Use `/bind {agent_id}` to route this chat to it."
    )
    return CommandResult(
        handled=True,
        response=(
            f"Created agent{loc}.\n{detail}\n{bind_hint}"
        ),
    )


_SUB_AGENT_IDENTITY_TEMPLATE = """\
# Identity

You are {agent_name}, a personal assistant sub-agent of Pip-Boy, powered by {model_name}.
You are a coding agent working in {workdir} that helps the USER with software engineering tasks.
Your main goal is to follow the USER's instructions, which are wrapped in `<user_query>` tags.
"""


def _replace_identity_section(body: str, new_identity: str) -> str:
    """Swap the first ``#… Identity`` section for ``new_identity``.

    The section runs from the Identity heading up to (but not
    including) the next heading at the **same** depth (``# `` for
    scaffold-style bodies, ``## `` for legacy). Any sub-headings
    below Identity (e.g. ``## Identity Recognition`` under ``#
    Identity Recognition`` — distinct by word, not depth) are
    preserved elsewhere because the regex anchors on the word
    ``Identity`` followed by a word-boundary, not an open-ended
    prefix match.

    If no Identity heading is found, the new identity text is
    prepended, so callers always end up with a valid Identity
    section at the top.
    """
    import re

    m = re.search(r"^(#+)\s+Identity\b[^\n]*\n", body, flags=re.MULTILINE)
    if not m:
        return new_identity.rstrip() + "\n\n" + body.lstrip()

    level = m.group(1)
    start = m.start()
    tail = body[m.end():]
    # Next heading at the same depth ends the section.
    nxt = re.search(rf"^{re.escape(level)}\s+\S", tail, flags=re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(body)

    head = body[:start].rstrip()
    rest = body[end:].lstrip()
    parts = [p for p in (head, new_identity.rstrip(), rest) if p]
    return "\n\n".join(parts) + ("\n" if rest else "")


def _create_agent_on_disk(
    registry: AgentRegistry,
    agent_id: str,
    *,
    dirname: str = "",
    name: str = "",
    model: str = "",
    dm_scope: str = "",
) -> tuple[AgentConfig | None, str | None]:
    """Materialise a new sub-agent directory + registry entry.

    ``dirname`` is the workspace-root-relative directory that owns the
    agent's ``.pip/``. It can differ from ``agent_id`` — in that case
    the registry records the mapping and the agent is reachable by
    either key. Defaults to ``agent_id`` when empty.

    The new agent inherits the default (``pip-boy``) agent's **style**
    sections (Identity, Core Philosophy, Tone And Style) from its
    persona.  System rules (System Communication, Memory, Identity
    Recognition) and work rules (Tool Calling, Making Code Changes,
    Git) are **not** copied — they live in workspace-shared files
    (``system_rules.md`` / ``work_rules.md``) and are injected at
    prompt-compose time.

    Only the ``# Identity`` section is rewritten, to flag the
    sibling relationship with Pip-Boy. The identity body still
    references ``{agent_name}`` / ``{model_name}`` / ``{workdir}``
    as template variables — they are resolved at prompt-compose time
    by :meth:`AgentConfig.system_prompt` from the YAML frontmatter,
    so editing ``name:`` in ``persona.md`` is enough to change how
    the agent refers to itself.

    Returns ``(cfg, None)`` on success or ``(None, error_msg)``.
    """
    import shutil
    from dataclasses import replace

    if registry.workspace_root is None:
        return (
            None,
            f"Cannot create '{agent_id}': workspace root is not configured.",
        )

    default = registry.default_agent()
    default_paths = registry.paths_for(default.id)
    body = _replace_identity_section(
        default.system_body or "", _SUB_AGENT_IDENTITY_TEMPLATE,
    )
    cfg = replace(
        default,
        id=agent_id,
        name=name or agent_id,
        system_body=body,
        model=model,
        dm_scope=dm_scope,
    )

    registry.register_agent(cfg, dirname=dirname or agent_id)
    new_paths = registry.paths_for(cfg.id)
    if new_paths is None:
        return None, f"Failed to allocate paths for agent '{agent_id}'."

    _persist_agent_md(cfg, new_paths.pip_dir)

    if default_paths is not None:
        default_hb = default_paths.pip_dir / "HEARTBEAT.md"
        new_hb = new_paths.pip_dir / "HEARTBEAT.md"
        if default_hb.is_file() and not new_hb.exists():
            new_hb.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(default_hb, new_hb)

    try:
        registry.save_registry()
    except Exception:
        log.exception("save_registry failed after creating agent %r", agent_id)

    return cfg, None


def _agent_archive(ctx: CommandContext, tail: list[str]) -> CommandResult:
    if not tail:
        return CommandResult(
            handled=True, response="Usage: /subagent archive <id>",
        )
    agent_id = normalize_agent_id(tail[0])
    default_id = ctx.registry.default_agent().id
    if agent_id == default_id:
        return CommandResult(
            handled=True, response="Cannot archive the root agent.",
        )
    if ctx.registry.get_agent(agent_id) is None:
        return CommandResult(
            handled=True, response=f"Unknown agent '{agent_id}'.",
        )
    paths_before = ctx.registry.paths_for(agent_id)
    dest = ctx.registry.archive_agent(agent_id)
    _purge_bindings_for(ctx, agent_id)
    if ctx.invalidate_agent is not None:
        ctx.invalidate_agent(agent_id)
    cc_removed: Path | None = None
    if paths_before is not None:
        cc_removed = _purge_cc_project_dir(paths_before.cwd)
    try:
        ctx.registry.save_registry()
    except Exception:
        log.exception("save_registry failed after /subagent archive")

    cc_note = (
        f"\nAlso purged CC project dir: {cc_removed}." if cc_removed else ""
    )
    if dest is None:
        return CommandResult(
            handled=True,
            response=(
                f"Archived agent '{agent_id}' (no .pip/ on disk).{cc_note}"
            ),
        )
    return CommandResult(
        handled=True,
        response=(
            f"Archived agent '{agent_id}': moved .pip/ to {dest}.\n"
            "Project files in the agent's directory are untouched."
            f"{cc_note}"
        ),
    )


def _agent_delete(ctx: CommandContext, tail: list[str]) -> CommandResult:
    """``/subagent delete <id> --yes`` — purge identity surface only."""
    if not tail:
        return CommandResult(
            handled=True, response="Usage: /subagent delete <id> --yes",
        )
    confirmed = "--yes" in tail
    positional = [t for t in tail if not t.startswith("--")]
    if not positional:
        return CommandResult(
            handled=True, response="Usage: /subagent delete <id> --yes",
        )
    agent_id = normalize_agent_id(positional[0])
    default_id = ctx.registry.default_agent().id
    if agent_id == default_id:
        return CommandResult(
            handled=True, response="Cannot delete the root agent.",
        )
    if ctx.registry.get_agent(agent_id) is None:
        return CommandResult(
            handled=True, response=f"Unknown agent '{agent_id}'.",
        )
    if not confirmed:
        return CommandResult(
            handled=True,
            response=(
                f"Refusing to delete '{agent_id}' without --yes.\n"
                "This wipes the agent's .pip/ (persona, memory, "
                "observations). Project files in the directory are "
                "kept. Use `/subagent archive {id}` for a reversible move."
            ),
        )

    paths_before = ctx.registry.paths_for(agent_id)
    removed = ctx.registry.remove_agent(agent_id, delete_files=True)
    _purge_bindings_for(ctx, agent_id)
    if ctx.invalidate_agent is not None:
        ctx.invalidate_agent(agent_id)
    cc_removed: Path | None = None
    if paths_before is not None:
        cc_removed = _purge_cc_project_dir(paths_before.cwd)
    try:
        ctx.registry.save_registry()
    except Exception:
        log.exception("save_registry failed after /subagent delete")

    if not removed:
        return CommandResult(
            handled=True, response=f"Nothing removed for '{agent_id}'.",
        )
    cc_note = (
        f"\nAlso purged CC project dir: {cc_removed}." if cc_removed else ""
    )
    return CommandResult(
        handled=True,
        response=(
            f"Deleted agent '{agent_id}' (wiped .pip/). "
            "Project files in the agent's directory are untouched."
            f"{cc_note}"
        ),
    )


def _cmd_bind(ctx: CommandContext, args: str) -> CommandResult:
    """``/bind <id>`` — route this chat to sub-agent ``<id>``.

    Works from anywhere — including from one sub-agent directly to
    another, without round-tripping through pip-boy. It mutates this
    chat's binding row only; sibling lifecycle (create/archive/
    delete/reset) still lives under ``/subagent`` and stays pip-boy
    only.

    Input is run through :func:`normalize_agent_id` so the user can
    type the directory name (``/bind helper``), a mixed-case variant
    (``/bind Helper``), or even a quoted multi-word label
    (``/bind "project stella"`` → ``project-stella``). Quoted args
    are parsed via ``shlex`` to honour embedded spaces.

    ``/bind pip-boy`` is rejected with a redirect to ``/unbind``, so
    "on pip-boy" has exactly one canonical representation (no binding
    row) rather than two (absent row vs explicit row pointing at
    root).
    """
    try:
        tail = shlex.split(args) if args.strip() else []
    except ValueError as exc:
        return CommandResult(handled=True, response=f"Parse error: {exc}")

    if not tail:
        ids = [
            cfg.id
            for cfg in ctx.registry.list_agents()
            if cfg.id != ctx.registry.default_agent().id
        ]
        known = ", ".join(sorted(ids)) if ids else "(none)"
        return CommandResult(
            handled=True,
            response=(
                "Usage: /bind <id>\n"
                f"Known sub-agents: {known}"
            ),
        )
    if len(tail) > 1:
        return CommandResult(
            handled=True,
            response=(
                "Usage: /bind <id>  (one argument; quote multi-word "
                "labels, e.g. `/bind \"project stella\"`)"
            ),
        )

    normalized = normalize_agent_id(tail[0])
    default_id = ctx.registry.default_agent().id
    if normalized == default_id:
        return CommandResult(
            handled=True,
            response=(
                f"`/bind {default_id}` is not supported — "
                "'on pip-boy' means 'no binding', not 'binding to root'. "
                "Use `/unbind` to clear the current binding instead."
            ),
        )
    # Lookup order: agent_id first (registry key), dirname as fallback.
    # Both resolution paths use the normalized form, so mixed-case
    # input (``/bind Foo``) resolves the same as the canonical form.
    agent = ctx.registry.get_agent(normalized)
    matched_via = "id"
    if agent is None:
        agent = ctx.registry.get_by_dirname(normalized)
        if agent is not None and agent.id != default_id:
            matched_via = "dir"
        else:
            agent = None
    if agent is None:
        known = ", ".join(
            sorted(
                cfg.id
                for cfg in ctx.registry.list_agents()
                if cfg.id != default_id
            )
        )
        return CommandResult(
            handled=True,
            response=(
                f"Unknown agent '{normalized}'.\n"
                f"Known sub-agents: {known}\n"
                f"Use `/subagent create {normalized}` to make one first "
                "(from pip-boy)."
            ),
        )
    agent_id = agent.id
    if matched_via == "dir":
        log.debug(
            "/bind matched %r via dirname; routing to agent %r",
            normalized, agent_id,
        )

    inbound = ctx.inbound
    if inbound.is_group:
        if not inbound.guild_id:
            return CommandResult(
                handled=True,
                response="Cannot /bind in group: missing guild_id.",
            )
        match_key, match_value = "guild_id", inbound.guild_id
    else:
        match_key, match_value = "peer_id", inbound.peer_id

    # Drop any existing binding at this (key, value) first so we
    # don't end up with stale rows when the chat was previously
    # routed elsewhere.
    ctx.bindings.remove(match_key, match_value)

    tier = 2 if inbound.is_group else 1
    binding = Binding(
        agent_id=agent_id,
        tier=tier,
        match_key=match_key,
        match_value=match_value,
    )
    ctx.bindings.add(binding)
    ctx.bindings.save(ctx.bindings_path)
    return CommandResult(
        handled=True,
        response=f"Bound to {agent.name or agent.id} ({agent_id}).",
    )


# ---------------------------------------------------------------------------
# /subagent reset — backup · delete · rebuild · restore
# ---------------------------------------------------------------------------
#
# What counts as "identity" (always preserved, copied into the rebuilt
# .pip/):
_RESET_PRESERVE_FILES = ("persona.md", "HEARTBEAT.md")


def _agent_reset(ctx: CommandContext, tail: list[str]) -> CommandResult:
    """``/subagent reset <id>`` — rebuild sub-agent ``<id>``'s .pip/ from a minimal backup.

    Algorithm (per the design note in the identity-redesign thread):

        1. Stash the "identity" files (persona.md, HEARTBEAT.md) to a
           sibling temp directory.
        2. Delete the agent's entire .pip/ directory.
        3. Recreate an empty .pip/ and restore the stash into it.
        4. Remove the temp stash.

    Outcome: persona + identity preserved, memory layer and any
    other bookkeeping files (observations, memories.json, axioms.md,
    state.json, incoming/, cron.json, sdk_sessions entries for this
    agent, .scaffold_manifest.json, ...) wiped and left to be lazily
    re-created by the running host.

    Root (pip-boy) refusal
    ----------------------
    ``/subagent reset pip-boy`` is rejected outright. The root agent's
    ``.pip/`` carries workspace-shared state (``addressbook/``,
    ``bindings.json``, ``agents_registry.json``, ``credentials/``,
    ``archived/``) AND its ``MemoryStore`` / ``StreamingSession`` are
    in active use by the very handler that would perform the reset.
    Any in-process "self-surgery" leaves a window where the cached
    store points at wiped paths, sessions hold file handles against
    CC's project dir, and ``sdk_sessions.json`` / ``bindings.json``
    can be resurrected by a concurrent write. If you really need to
    reset pip-boy, stop the host (``/exit``) and rebuild the root
    ``.pip/`` offline, then restart.

    Workspace ``sdk_sessions.json`` is shared across agents; only the
    entries keyed to the reset agent are removed.
    """
    import json
    import shutil
    import tempfile

    if len(tail) != 1:
        ids = ", ".join(sorted(cfg.id for cfg in ctx.registry.list_agents()))
        return CommandResult(
            handled=True,
            response=(
                "Usage: /subagent reset <id>\n"
                f"Known agents: {ids}"
            ),
        )

    agent_id = normalize_agent_id(tail[0])
    default_id = ctx.registry.default_agent().id
    if agent_id == default_id:
        return CommandResult(
            handled=True,
            response=(
                f"Cannot reset the root agent '{default_id}' from within "
                "the running host. Its memory store and session are in "
                "active use by this very command, and its .pip/ holds "
                "workspace-shared state (addressbook/, bindings.json, "
                "agents_registry.json, credentials/, archived/) that "
                "other agents rely on. Stop the host (/exit) and "
                "rebuild the root .pip/ offline if you really need to."
            ),
        )

    agent = ctx.registry.get_agent(agent_id)
    if agent is None:
        return CommandResult(
            handled=True, response=f"Unknown agent '{agent_id}'.",
        )
    paths = ctx.registry.paths_for(agent_id)
    if paths is None:
        return CommandResult(
            handled=True,
            response=f"[error] agent {agent_id!r} has no resolvable paths.",
        )

    pip_dir = paths.pip_dir

    if not pip_dir.is_dir():
        # Nothing to reset; treat as a no-op success rather than
        # erroring, so the operator can use this as a "make sure it
        # exists" idempotent action.
        pip_dir.mkdir(parents=True, exist_ok=True)
        return CommandResult(
            handled=True,
            response=(
                f"Agent '{agent_id}' had no .pip/ on disk; created an "
                "empty one. Nothing to wipe."
            ),
        )

    preserve_files = list(_RESET_PRESERVE_FILES)

    # --- 1. Stash ---------------------------------------------------
    stash = Path(
        tempfile.mkdtemp(prefix=f"pip-reset-{agent_id}-", dir=pip_dir.parent)
    )
    try:
        for name in preserve_files:
            src = pip_dir / name
            if src.is_file():
                shutil.copy2(src, stash / name)

        # --- 2. Delete --------------------------------------------
        shutil.rmtree(pip_dir)

        # --- 3. Rebuild + restore --------------------------------
        pip_dir.mkdir(parents=True)
        # Re-seed the standard MemoryStore subdirs so the rebuilt
        # ``.pip/`` matches what a fresh ``MemoryStore.__init__``
        # produces. Without this, any cached per-agent service
        # (AgentHost._agents) keeps a MemoryStore whose directories
        # no longer exist, and the first reflect after reset dies
        # with ENOENT on ``observations/<date>.jsonl``. Addressbook
        # lives at the workspace root and is deliberately NOT touched
        # here — a sub-agent reset must not nuke shared contacts.
        (pip_dir / "observations").mkdir(exist_ok=True)
        for name in preserve_files:
            staged = stash / name
            if staged.is_file():
                shutil.copy2(staged, pip_dir / name)
    finally:
        # --- 4. Drop the stash (even on failure) -----------------
        shutil.rmtree(stash, ignore_errors=True)

    # --- in-memory caches + sdk_sessions.json cleanup -------------
    # When the host is wired up, a single callback drops both the
    # cached per-agent service (AgentHost._agents) and the agent's
    # session rows (AgentHost._sessions + sdk_sessions.json) — that
    # keeps the live map and the on-disk file consistent and prevents
    # a stale MemoryStore from resurrecting ``.pip/`` with
    # ``state.json`` after the reset. When no host is wired (unit
    # tests that build CommandContext directly), fall back to a
    # direct file edit so the sdk_sessions invariant still holds.
    if ctx.invalidate_agent is not None:
        ctx.invalidate_agent(agent_id)
    else:
        sessions_path = paths.workspace_pip_dir / "sdk_sessions.json"
        if sessions_path.is_file():
            try:
                blob = json.loads(sessions_path.read_text(encoding="utf-8"))
                if isinstance(blob, dict):
                    prefix = f"agent:{agent_id}:"
                    cleaned = {
                        k: v for k, v in blob.items() if not k.startswith(prefix)
                    }
                    if cleaned != blob:
                        sessions_path.write_text(
                            json.dumps(cleaned, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
            except Exception:
                log.exception(
                    "sdk_sessions.json cleanup failed for reset of %r", agent_id,
                )

    # --- CC-side cleanup ----------------------------------------
    # The memory layer wipe is incomplete without also clearing
    # Claude Code's per-project cache at ``~/.claude/projects/<cwd>/``
    # (session JSONLs + CC's own ``memory/`` cards). If we leave it
    # behind, the very next turn of the "reset" agent can rehydrate
    # the wiped identity via CC's native recall and make reset a
    # no-op from the user's POV.
    cc_removed = _purge_cc_project_dir(paths.cwd)

    cc_note = (
        f"\nAlso purged CC project dir: {cc_removed}." if cc_removed else ""
    )
    return CommandResult(
        handled=True,
        response=(
            f"Reset agent '{agent_id}'. Preserved: persona.md, HEARTBEAT.md. "
            "Memory (observations, memories, axioms, state) and "
            "per-agent bookkeeping were wiped."
            f"{cc_note}"
        ),
    )


_SUBAGENT_SUBCOMMANDS: dict[str, Any] = {
    "list": _agent_list,
    "create": _agent_create,
    "archive": _agent_archive,
    "delete": _agent_delete,
    "reset": _agent_reset,
}


# ---------------------------------------------------------------------------
# /unbind — the counterpart of /bind: clears this chat's binding so
# routing falls back to pip-boy
# ---------------------------------------------------------------------------


def _cmd_unbind(ctx: CommandContext, _args: str) -> CommandResult:
    """``/unbind`` — clear this chat's binding and fall back to pip-boy.

    Works from any sub-agent. Removes the binding row that's
    currently routing this chat; with no row, routing falls back to
    the default agent (pip-boy) via the normal resolver fallback.
    Running ``/unbind`` while already on pip-boy is a friendly no-op
    so the command is safe to hit repeatedly.
    """
    current_id = _resolved_agent_id(ctx)
    root_id = ctx.registry.default_agent().id
    if current_id == root_id:
        return CommandResult(
            handled=True,
            response=f"Already on {root_id}. Nothing to do.",
        )

    inbound = ctx.inbound
    if inbound.is_group:
        if not inbound.guild_id:
            return CommandResult(
                handled=True,
                response="Cannot /unbind in group: missing guild_id.",
            )
        match_key, match_value = "guild_id", inbound.guild_id
    else:
        match_key, match_value = "peer_id", inbound.peer_id

    removed = ctx.bindings.remove(match_key, match_value)
    if removed:
        try:
            ctx.bindings.save(ctx.bindings_path)
        except Exception:
            log.exception("Failed to persist bindings after /unbind")
    return CommandResult(
        handled=True,
        response=f"Unbound. Routing falls back to {root_id}.",
    )


# ---------------------------------------------------------------------------
# (Legacy handlers /agents, /create-agent, /archive-agent, /delete-agent,
# /switch, /reset removed — functionality consolidated into
# `/subagent <subcommand>`, `/bind`, `/unbind` above. The earlier
# ``/agent`` umbrella was renamed to ``/subagent`` so the verb matches
# what it actually does (manage siblings), and the asymmetric
# ``/agent switch`` + ``/home`` pair was replaced by the symmetric
# ``/bind`` / ``/unbind`` pair that works from any agent.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /wechat — multi-account lifecycle (CLI-only)
# ---------------------------------------------------------------------------


_WECHAT_USAGE = (
    "Usage:\n"
    "  /wechat list                   — show registered accounts + bindings\n"
    "  /wechat add <agent_id>         — start QR login, bind new account to <agent_id>\n"
    "  /wechat cancel                 — abort an in-progress QR login\n"
    "  /wechat remove <id>            — stop polling, delete credential + binding\n"
    "                                   (<id> = account_id [removes one]\n"
    "                                        or agent_id  [detaches all of its accounts])"
)


def _cmd_wechat(ctx: CommandContext, args: str) -> CommandResult:
    """``/wechat`` command family.

    CLI-only (the ACL in :func:`dispatch_command` already enforces
    that). When WeChat isn't configured for this run, ``ctx
    .wechat_controller`` is ``None`` and every sub-command responds
    with a one-line hint instead of silently failing.
    """
    controller = ctx.wechat_controller
    tail = shlex.split(args) if args else []
    sub = tail[0].lower() if tail else ""
    rest = tail[1:]

    # ``add`` is the bootstrap entry point — when the WeChat stack
    # hasn't been built yet (no valid tier-3 bindings at boot),
    # construct it now via the host callback. This removes the need
    # for a launch flag: a fresh install can ``/wechat add <agent_id>``
    # to scan its first account without restarting.
    if controller is None and sub == "add" and ctx.ensure_wechat_controller is not None:
        try:
            controller = ctx.ensure_wechat_controller()
        except Exception as exc:  # noqa: BLE001
            return CommandResult(
                handled=True,
                response=f"WeChat init failed: {exc}",
            )

    if controller is None:
        return CommandResult(
            handled=True,
            response=(
                "WeChat channel is not active for this run. "
                "Run `/wechat add <agent_id>` to scan a new account in, "
                "or drop a credential file under "
                "`.pip/credentials/wechat/` and restart."
            ),
        )

    if sub == "list":
        rows = controller.list_accounts()
        if not rows:
            return CommandResult(
                handled=True,
                response=(
                    "## WeChat accounts\n\n"
                    "*(No accounts registered.)*\n\n"
                    "Use `/wechat add <agent_id>` to scan one in."
                ),
            )
        lines = [
            f"## WeChat accounts ({len(rows)})",
            "",
            "| Account id | Agent id | Logged in |",
            "| --- | --- | --- |",
        ]
        for row in rows:
            agent = row["agent_id"] or "(unbound)"
            aid = str(row.get("account_id") or "")
            li = str(row.get("logged_in") or "")
            lines.append(
                f"| {_md_table_cell(aid)} | {_md_table_cell(agent)} | "
                f"{_md_table_cell(li)} |"
            )
        current = controller.current_qr_agent()
        if current:
            lines.append("")
            lines.append(f"*QR scan in progress for agent:* `{current}`")
        return CommandResult(handled=True, response="\n".join(lines))

    if sub == "add":
        if not rest:
            return CommandResult(
                handled=True,
                response="Usage: /wechat add <agent_id>",
            )
        accepted, message = controller.start_qr_login(rest[0])
        return CommandResult(handled=True, response=message)

    if sub == "cancel":
        did = controller.cancel_qr()
        return CommandResult(
            handled=True,
            response=(
                "QR login cancelled."
                if did else "No QR login in progress."
            ),
        )

    if sub == "remove":
        if not rest:
            return CommandResult(
                handled=True,
                response="Usage: /wechat remove <account_id|agent_id>",
            )
        target = rest[0]
        # Preferred path: ``target`` is an account_id (the canonical
        # primary key, what ``/wechat list`` displays on the LHS).
        # account_id → agent_id is globally unique because
        # ``WeChatController._qr_worker`` always rewrites the binding on
        # re-scan, so an account_id resolves to at most one binding.
        if controller.remove_account(target):
            return CommandResult(
                handled=True,
                response=f"Removed account {target} (credential + binding).",
            )
        # Fallback: ``target`` may be an agent_id. The ``/wechat list``
        # output shows ``account_id -> agent_id``; users naturally try
        # the RHS too. Semantics: detach **every** WeChat account from
        # this agent (one agent → many accounts is allowed). We list
        # what was removed so the operator can see the blast radius.
        bound = [
            b.match_value
            for b in ctx.bindings.list_all()
            if b.tier == 3
            and b.match_key == "account_id"
            and b.agent_id == target
            and (b.match_value or "").strip()
        ]
        if not bound:
            return CommandResult(
                handled=True,
                response=(
                    f"No account or binding found for {target!r}. "
                    "Run `/wechat list` to see registered accounts."
                ),
            )
        removed = [aid for aid in bound if controller.remove_account(aid)]
        if not removed:
            return CommandResult(
                handled=True,
                response=f"No accounts removed for agent {target!r}.",
            )
        plural = "s" if len(removed) > 1 else ""
        ids = ", ".join(removed)
        return CommandResult(
            handled=True,
            response=(
                f"Removed {len(removed)} account{plural} bound to "
                f"agent {target!r}: {ids} (credential + binding)."
            ),
        )

    return CommandResult(handled=True, response=_WECHAT_USAGE)


# ---------------------------------------------------------------------------
# /theme — TUI theme listing / selection / rescan
# ---------------------------------------------------------------------------
#
# Theme handling lives in :mod:`pip_agent.tui` (data + App hot-swap) and
# :mod:`pip_agent.host_state` (persistence). ``/theme set`` applies the
# bundle to the live TUI via ``PipBoyTuiApp.apply_theme`` and persists
# the slug to ``host_state.json`` — no restart required. In line mode
# (no TUI) ``/theme set`` still persists so the next TUI boot honours
# it. ``/theme refresh`` re-walks ``<workspace>/.pip/themes/`` so
# operators can edit or drop in a new theme without bouncing pip-boy.

_THEME_USAGE = (
    "Usage:\n"
    "  /theme list                — show installed themes\n"
    "  /theme set <name>          — switch theme immediately + persist\n"
    "  /theme refresh             — rescan .pip/themes/ for new/edited themes\n"
    "\n"
    "Themes live at <workspace>/.pip/themes/<slug>/. Pip-Boy seeds a few "
    "examples on first boot; feel free to edit or delete them."
)


def _cmd_theme(ctx: CommandContext, args: str) -> CommandResult:
    """Dispatcher for the ``/theme`` family — flat subcommands."""
    if ctx.theme_manager is None:
        return CommandResult(
            handled=True,
            response=(
                "Theme manager is not active in this run. "
                "(Likely line-mode boot or a unit-test context.)"
            ),
        )

    try:
        tokens = shlex.split(args) if args.strip() else []
    except ValueError as exc:
        return CommandResult(handled=True, response=f"Parse error: {exc}")

    if not tokens or tokens[0].lower() in {"help", "--help", "-h"}:
        return CommandResult(handled=True, response=_THEME_USAGE)

    sub = tokens[0].lower()
    tail = tokens[1:]
    if sub == "list":
        return _theme_list(ctx, tail)
    if sub == "set":
        return _theme_set(ctx, tail)
    if sub == "refresh":
        return _theme_refresh(ctx, tail)

    from difflib import get_close_matches

    hint = get_close_matches(sub, ["list", "set", "refresh"], n=1, cutoff=0.6)
    suffix = f" Did you mean `/theme {hint[0]}`?" if hint else ""
    return CommandResult(
        handled=True,
        response=(
            f"Unknown /theme subcommand '{sub}'.{suffix}\n{_THEME_USAGE}"
        ),
    )


def _theme_list(ctx: CommandContext, _tail: list[str]) -> CommandResult:
    mgr = ctx.theme_manager
    assert mgr is not None  # checked by _cmd_theme
    snapshot = mgr.discover()
    bundles = list(snapshot.bundles.values())

    if not bundles and not snapshot.issues:
        return CommandResult(
            handled=True,
            response=(
                "No themes available. Drop a valid theme directory under "
                "<workspace>/.pip/themes/<slug>/ or run `/theme refresh`."
            ),
        )

    bundles.sort(key=lambda b: b.manifest.name)
    lines = [f"Themes ({len(bundles)}):"]
    for bundle in bundles:
        marker = " *" if bundle.manifest.name == ctx.active_theme_name else ""
        has_art = bool(bundle.art_frames)
        truncated = (
            f" ({bundle.art_frame_height}x{bundle.art_frame_width} art)"
            if has_art else ""
        )
        lines.append(
            f"  {bundle.manifest.name}{marker}"
            f" — {bundle.manifest.display_name}"
            f" v{bundle.manifest.version}"
            f"{truncated}"
        )
    if snapshot.issues:
        lines.append("")
        lines.append(f"Skipped ({len(snapshot.issues)}):")
        for issue in snapshot.issues:
            head = issue.reason.splitlines()[0] if issue.reason else "(no detail)"
            lines.append(f"  {issue.path.name} — {head}")
    lines.append("")
    lines.append("* = currently active. Use `/theme set <name>` to switch.")
    return CommandResult(handled=True, response="\n".join(lines))


def _theme_set(ctx: CommandContext, tail: list[str]) -> CommandResult:
    if len(tail) != 1:
        return CommandResult(
            handled=True,
            response="Usage: /theme set <name>",
        )
    if ctx.host_state is None:
        return CommandResult(
            handled=True,
            response=(
                "Cannot persist theme: host_state is unavailable. "
                "(Likely a unit-test context.)"
            ),
        )
    name = tail[0].strip()
    mgr = ctx.theme_manager
    assert mgr is not None
    bundle = mgr.get(name)
    if bundle is None:
        snapshot = mgr.discover()
        known = ", ".join(sorted(snapshot.bundles)) or "(none)"
        return CommandResult(
            handled=True,
            response=(
                f"Unknown theme '{name}'. "
                f"Known: {known}. Run `/theme list` for full detail, "
                f"or `/theme refresh` after dropping in a new one."
            ),
        )

    try:
        ctx.host_state.set_theme(bundle.manifest.name)
    except OSError as exc:
        log.exception("Failed to persist theme preference")
        return CommandResult(
            handled=True, response=f"Failed to write host_state: {exc}",
        )

    applied_live = False
    if ctx.tui_app is not None:
        try:
            ctx.tui_app.call_later(ctx.tui_app.apply_theme, bundle)
            applied_live = True
        except Exception as exc:  # noqa: BLE001 — never fail /theme set
            log.exception("Live theme apply failed; persisted only.")
            return CommandResult(
                handled=True,
                response=(
                    f"Persisted theme '{bundle.manifest.name}' but live "
                    f"apply failed: {exc}. Restart to pick up the new theme."
                ),
            )

    if ctx.set_active_theme is not None:
        try:
            ctx.set_active_theme(bundle.manifest.name)
        except Exception:  # noqa: BLE001
            log.exception("set_active_theme callback raised; ignoring.")

    if applied_live:
        msg = (
            f"Theme → '{bundle.manifest.name}' "
            f"(\"{bundle.manifest.display_name}\"). Applied live."
        )
    else:
        msg = (
            f"Persisted theme '{bundle.manifest.name}' "
            f"(\"{bundle.manifest.display_name}\"). "
            f"No TUI attached; will apply on next boot."
        )
    return CommandResult(handled=True, response=msg)


def _theme_refresh(ctx: CommandContext, _tail: list[str]) -> CommandResult:
    mgr = ctx.theme_manager
    assert mgr is not None
    prev_snapshot = mgr.snapshot() or mgr.discover()
    prev_slugs = set(prev_snapshot.bundles)

    new_snapshot = mgr.discover()
    new_slugs = set(new_snapshot.bundles)

    added = sorted(new_slugs - prev_slugs)
    removed = sorted(prev_slugs - new_slugs)
    broken = new_snapshot.issues

    lines = [
        f"Rescanned .pip/themes/: +{len(added)} new, -{len(removed)} removed, "
        f"{len(broken)} broken."
    ]
    if added:
        lines.append("  Added: " + ", ".join(added))
    if removed:
        lines.append("  Removed: " + ", ".join(removed))
        if ctx.active_theme_name and ctx.active_theme_name in removed:
            lines.append(
                f"  ! Active theme '{ctx.active_theme_name}' was removed "
                f"from disk but is still running in memory."
            )
    if broken:
        lines.append(f"  Broken ({len(broken)}):")
        for issue in broken:
            head = issue.reason.splitlines()[0] if issue.reason else "(no detail)"
            lines.append(f"    {issue.path.name} — {head}")
    if added or removed or broken:
        lines.append("Run `/theme set <slug>` to switch.")
    else:
        lines.append("No changes.")
    return CommandResult(handled=True, response="\n".join(lines))


def _cmd_exit(ctx: CommandContext, _args: str) -> CommandResult:
    if ctx.inbound.channel == "cli":
        # Belt-and-braces: the CLI loop intercepts /exit before dispatch,
        # so we shouldn't normally land here. If we do, treat it as a
        # friendly no-op instead of quietly doing nothing.
        return CommandResult(
            handled=True,
            response="Use /exit at the CLI prompt directly.",
        )
    return CommandResult(
        handled=True,
        response="/exit is only available at the CLI.",
    )


# ---------------------------------------------------------------------------
# /plugin — Claude Code plugin / marketplace management
# ---------------------------------------------------------------------------
#
# Why this lives on the host
# --------------------------
# Plugin install / uninstall mutates ``~/.claude/`` (or
# ``<cwd>/.claude/``) which is the same state the SDK subprocess
# reads on the next ``query()``. We don't cache it; ``plugins.py``
# just shells out to the bundled ``claude.exe``. The host wrapper
# exists so:
#
# * users can install plugins from chat (``/plugin install foo``)
#   without leaving the conversation,
# * three install scopes (``user`` / ``project`` / ``local``) are
#   surfaced consistently with Claude Code's own CLI,
# * destructive operations (``uninstall``, ``disable``, ``marketplace
#   remove``) stay on the host surface; the agent-facing MCP variant
#   intentionally only exposes additive ops (see ``mcp_tools._plugin_tools``).
#
# Scope semantics
# ---------------
# When ``--scope project`` or ``--scope local`` is given, the
# subprocess inherits the **active agent's** workdir as ``cwd`` so
# the resulting ``.claude/`` files land beside that agent's project,
# not the host process's cwd. Each Pip-Boy sub-agent therefore has its
# own per-project plugin set, just like CC's own per-project semantics.

_PLUGIN_USAGE = (
    "Usage:\n"
    "  /plugin list [--available]\n"
    "  /plugin search <query>\n"
    "  /plugin install <spec> [--scope user|project|local]\n"
    "  /plugin uninstall <name> [--scope user|project|local]\n"
    "  /plugin enable <name> [--scope user|project|local]\n"
    "  /plugin disable <name> [--scope user|project|local]\n"
    "  /plugin marketplace list\n"
    "  /plugin marketplace add <gh-repo|url|path> "
    "[--scope user|project|local]\n"
    "  /plugin marketplace remove <name>\n"
    "  /plugin marketplace update [name]\n"
    "  /plugin help\n"
    "\n"
    "<spec> is `<name>` or `<name>@<marketplace>` to disambiguate.\n"
    "Default scope is `user` (global). `project` writes to the active "
    "agent's `.claude/settings.json`; `local` writes to "
    "`.claude/settings.local.json` (gitignored). Plugins from any "
    "scope are picked up by the next agent turn."
)

_VALID_PLUGIN_SCOPES = {"user", "project", "local"}


def _parse_plugin_flags(
    tokens: list[str],
) -> tuple[list[str], dict[str, str], str | None]:
    """Split ``tokens`` into ``(positionals, flags, error)``.

    Recognised flags: ``--scope``. Unrecognised ``--foo`` is rejected
    so typos like ``--scop project`` fail fast instead of silently
    going to the user-default scope.
    """
    allowed = {"--scope"}
    positionals: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            if tok not in allowed:
                return [], {}, f"Unknown flag '{tok}'."
            if i + 1 >= len(tokens):
                return [], {}, f"Flag '{tok}' needs a value."
            value = tokens[i + 1]
            if tok == "--scope" and value not in _VALID_PLUGIN_SCOPES:
                return [], {}, (
                    f"Invalid scope '{value}'. "
                    "Valid: user, project, local."
                )
            flags[tok] = value
            i += 2
            continue
        positionals.append(tok)
        i += 1
    return positionals, flags, None


def _active_agent_cwd(ctx: CommandContext) -> Path:
    """Resolve the active agent's effective ``cwd``.

    Used as the subprocess ``cwd`` for ``project`` / ``local`` scoped
    operations so each sub-agent has its own per-project plugin set.
    Falls back to ``Path.cwd()`` when registry paths aren't wired (unit
    tests, very early bootstrap).
    """
    aid = _resolved_agent_id(ctx)
    paths = ctx.registry.paths_for(aid)
    if paths is not None:
        return paths.cwd
    return Path.cwd()


def _truncate(s: str, limit: int = 60) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "\u2026"


def _md_table_cell(s: str, *, limit: int = 72) -> str:
    """Escape ``|`` and cap width so GFM tables stay parseable in the TUI."""
    t = _truncate(str(s or ""), limit=limit).replace("|", "\\|")
    return t.replace("\r", "")


def _format_plugin_list(items: list[dict], available: bool) -> str:
    if not items:
        if available:
            return (
                "## Available plugins\n\n"
                "*(No plugins in the catalogue.)*\n\n"
                "Add a marketplace with `/plugin marketplace add "
                "<gh-repo|url|path>`."
            )
        return (
            "## Installed plugins\n\n"
            "*(No plugins installed.)*\n\n"
            "Run `/plugin list --available` to browse the catalogue."
        )

    label = "Available" if available else "Installed"
    lines = [
        f"## {label} plugins ({len(items)})",
        "",
        "| Plugin | Scope | Marketplace | Description |",
        "| --- | --- | --- | --- |",
    ]
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("id") or it.get("pluginId") or "?"
        scope_raw = it.get("scope")
        scope = scope_raw if isinstance(scope_raw, str) else ""
        market_raw = (
            it.get("marketplaceName")
            or it.get("marketplace")
            or it.get("source_marketplace")
        )
        market = market_raw if isinstance(market_raw, str) else ""
        enabled = it.get("enabled")
        flag = " [disabled]" if enabled is False else ""
        desc = str(it.get("description") or it.get("summary") or "")
        name_disp = f"{name}{flag}"
        lines.append(
            f"| {_md_table_cell(name_disp)} | {_md_table_cell(scope)} | "
            f"{_md_table_cell(market)} | {_md_table_cell(desc, limit=96)} |"
        )
    return "\n".join(lines)


def _format_marketplace_list(items: list[dict]) -> str:
    if not items:
        return (
            "## Marketplaces\n\n"
            "*(None configured.)*\n\n"
            "Add one with `/plugin marketplace add <gh-repo|url|path>`."
        )
    lines = [
        f"## Marketplaces ({len(items)})",
        "",
        "| Name | Scope | Plugins | Source |",
        "| --- | --- | --- | --- |",
    ]
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("id") or "?"
        src = (
            it.get("repo")
            or it.get("url")
            or it.get("path")
            or it.get("source")
            or ""
        )
        if not isinstance(src, str):
            src = ""
        scope = it.get("scope") if isinstance(it.get("scope"), str) else ""
        plug_count = it.get("pluginCount") or it.get("plugins") or ""
        plug_s = str(plug_count) if plug_count not in (None, "") else ""
        lines.append(
            f"| {_md_table_cell(name)} | {_md_table_cell(scope)} | "
            f"{_md_table_cell(plug_s)} | {_md_table_cell(src, limit=96)} |"
        )
    return "\n".join(lines)


def _plugin_error_response(exc: Exception) -> CommandResult:
    """Render a plugin subprocess error as a tidy chat response."""
    from pip_agent.plugins import PluginsCLIError, PluginsCLINotFound

    if isinstance(exc, PluginsCLINotFound):
        return CommandResult(handled=True, response=str(exc))
    if isinstance(exc, PluginsCLIError):
        msg = (exc.stderr or exc.stdout or "").strip()
        head = msg.splitlines()[0] if msg else f"exit code {exc.returncode}"
        return CommandResult(
            handled=True,
            response=f"Plugin command failed: {head}",
        )
    log.exception("/plugin handler crashed")
    return CommandResult(handled=True, response=f"[error] {exc}")


def _plugin_list_handler(
    ctx: CommandContext, tail: list[str],
) -> CommandResult:
    from pip_agent import plugins as plug

    available = False
    rest: list[str] = []
    for tok in tail:
        if tok in ("--available", "-a"):
            available = True
        else:
            rest.append(tok)
    if rest:
        return CommandResult(
            handled=True,
            response=f"Unexpected argument: {rest[0]}\n{_PLUGIN_USAGE}",
        )
    cwd = _active_agent_cwd(ctx)
    try:
        items = plug.run_sync(plug.plugin_list(available=available, cwd=cwd))
    except Exception as exc:  # noqa: BLE001
        return _plugin_error_response(exc)
    return CommandResult(
        handled=True,
        response=_format_plugin_list(items, available=available),
    )


def _plugin_search_handler(
    ctx: CommandContext, tail: list[str],
) -> CommandResult:
    from pip_agent import plugins as plug

    if not tail:
        return CommandResult(
            handled=True,
            response="Usage: /plugin search <query>",
        )
    query = " ".join(tail).strip()
    cwd = _active_agent_cwd(ctx)
    try:
        items = plug.run_sync(plug.plugin_search(query, cwd=cwd))
    except Exception as exc:  # noqa: BLE001
        return _plugin_error_response(exc)
    if not items:
        return CommandResult(
            handled=True,
            response=(
                f"No plugins matched '{query}'. "
                "Run `/plugin marketplace list` to see configured sources, "
                "or `/plugin list --available` for the full catalogue."
            ),
        )
    return CommandResult(
        handled=True,
        response=_format_plugin_list(items, available=True),
    )


def _plugin_install_handler(
    ctx: CommandContext, tail: list[str],
) -> CommandResult:
    from pip_agent import plugins as plug

    positionals, flags, err = _parse_plugin_flags(tail)
    if err:
        return CommandResult(handled=True, response=f"{err}\n{_PLUGIN_USAGE}")
    if len(positionals) != 1:
        return CommandResult(
            handled=True,
            response="Usage: /plugin install <spec> [--scope SCOPE]",
        )
    spec = positionals[0]
    scope = flags.get("--scope", "user")
    cwd = _active_agent_cwd(ctx)
    try:
        out, err_text, _ = plug.run_sync(
            plug.plugin_install(spec, scope=scope, cwd=cwd),  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        return _plugin_error_response(exc)
    body = (out or err_text).strip() or f"Installed {spec} (scope={scope})."
    return CommandResult(handled=True, response=body)


def _plugin_uninstall_handler(
    ctx: CommandContext, tail: list[str],
) -> CommandResult:
    from pip_agent import plugins as plug

    positionals, flags, err = _parse_plugin_flags(tail)
    if err:
        return CommandResult(handled=True, response=f"{err}\n{_PLUGIN_USAGE}")
    if len(positionals) != 1:
        return CommandResult(
            handled=True,
            response="Usage: /plugin uninstall <name> [--scope SCOPE]",
        )
    name = positionals[0]
    scope = flags.get("--scope")
    cwd = _active_agent_cwd(ctx)
    try:
        out, err_text, _ = plug.run_sync(
            plug.plugin_uninstall(name, scope=scope, cwd=cwd),  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        return _plugin_error_response(exc)
    body = (out or err_text).strip() or f"Uninstalled {name}."
    return CommandResult(handled=True, response=body)


def _plugin_enable_handler(
    ctx: CommandContext, tail: list[str],
) -> CommandResult:
    from pip_agent import plugins as plug

    positionals, flags, err = _parse_plugin_flags(tail)
    if err:
        return CommandResult(handled=True, response=f"{err}\n{_PLUGIN_USAGE}")
    if len(positionals) != 1:
        return CommandResult(
            handled=True,
            response="Usage: /plugin enable <name> [--scope SCOPE]",
        )
    name = positionals[0]
    scope = flags.get("--scope")
    cwd = _active_agent_cwd(ctx)
    try:
        out, err_text, _ = plug.run_sync(
            plug.plugin_enable(name, scope=scope, cwd=cwd),  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        return _plugin_error_response(exc)
    body = (out or err_text).strip() or f"Enabled {name}."
    return CommandResult(handled=True, response=body)


def _plugin_disable_handler(
    ctx: CommandContext, tail: list[str],
) -> CommandResult:
    from pip_agent import plugins as plug

    positionals, flags, err = _parse_plugin_flags(tail)
    if err:
        return CommandResult(handled=True, response=f"{err}\n{_PLUGIN_USAGE}")
    if len(positionals) != 1:
        return CommandResult(
            handled=True,
            response="Usage: /plugin disable <name> [--scope SCOPE]",
        )
    name = positionals[0]
    scope = flags.get("--scope")
    cwd = _active_agent_cwd(ctx)
    try:
        out, err_text, _ = plug.run_sync(
            plug.plugin_disable(name, scope=scope, cwd=cwd),  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        return _plugin_error_response(exc)
    body = (out or err_text).strip() or f"Disabled {name}."
    return CommandResult(handled=True, response=body)


def _plugin_marketplace_handler(
    ctx: CommandContext, tail: list[str],
) -> CommandResult:
    from pip_agent import plugins as plug

    if not tail:
        return CommandResult(
            handled=True,
            response="Usage: /plugin marketplace {list|add|remove|update} ...",
        )
    sub = tail[0].lower()
    rest = tail[1:]
    cwd = _active_agent_cwd(ctx)

    try:
        if sub == "list":
            items = plug.run_sync(plug.marketplace_list(cwd=cwd))
            return CommandResult(
                handled=True,
                response=_format_marketplace_list(items),
            )

        if sub == "add":
            positionals, flags, err = _parse_plugin_flags(rest)
            if err:
                return CommandResult(handled=True, response=f"{err}\n{_PLUGIN_USAGE}")
            if len(positionals) != 1:
                return CommandResult(
                    handled=True,
                    response=(
                        "Usage: /plugin marketplace add <gh-repo|url|path> "
                        "[--scope SCOPE]"
                    ),
                )
            source = positionals[0]
            scope = flags.get("--scope", "user")
            out, err_text, _ = plug.run_sync(
                plug.marketplace_add(source, scope=scope, cwd=cwd),  # type: ignore[arg-type]
            )
            body = (out or err_text).strip() or (
                f"Added marketplace {source} (scope={scope})."
            )
            return CommandResult(handled=True, response=body)

        if sub == "remove":
            if len(rest) != 1:
                return CommandResult(
                    handled=True,
                    response="Usage: /plugin marketplace remove <name>",
                )
            out, err_text, _ = plug.run_sync(
                plug.marketplace_remove(rest[0], cwd=cwd),
            )
            body = (out or err_text).strip() or f"Removed marketplace {rest[0]}."
            return CommandResult(handled=True, response=body)

        if sub == "update":
            name = rest[0] if rest else None
            out, err_text, _ = plug.run_sync(
                plug.marketplace_update(name, cwd=cwd),
            )
            body = (out or err_text).strip() or "Marketplace metadata refreshed."
            return CommandResult(handled=True, response=body)

    except Exception as exc:  # noqa: BLE001
        return _plugin_error_response(exc)

    return CommandResult(
        handled=True,
        response=(
            f"Unknown /plugin marketplace subcommand '{sub}'.\n{_PLUGIN_USAGE}"
        ),
    )


def _cmd_plugin(ctx: CommandContext, args: str) -> CommandResult:
    """Dispatcher for the ``/plugin`` family.

    Flat dispatch on the first token (mirrors ``/subagent``):
    ``list / search / install / uninstall / enable / disable /
    marketplace / help``. Marketplace operations are themselves a
    second-level dispatch (``/plugin marketplace list`` / ``add`` /
    ``remove`` / ``update``).

    Available on every channel — same trust model as ``/cron``: users
    own the plugin sources they trust.
    """
    try:
        tokens = shlex.split(args) if args.strip() else []
    except ValueError as exc:
        return CommandResult(handled=True, response=f"Parse error: {exc}")

    if not tokens or tokens[0].lower() == "help":
        return CommandResult(handled=True, response=_PLUGIN_USAGE)

    sub = tokens[0].lower()
    tail = tokens[1:]
    handler = _PLUGIN_SUBCOMMANDS.get(sub)
    if handler is None:
        from difflib import get_close_matches

        hint = get_close_matches(sub, _PLUGIN_SUBCOMMANDS.keys(), n=1, cutoff=0.6)
        suffix = f" Did you mean `/plugin {hint[0]}`?" if hint else ""
        return CommandResult(
            handled=True,
            response=(
                f"Unknown /plugin subcommand '{sub}'.{suffix}\n{_PLUGIN_USAGE}"
            ),
        )
    return handler(ctx, tail)


_PLUGIN_SUBCOMMANDS: dict[str, Any] = {
    "list": _plugin_list_handler,
    "search": _plugin_search_handler,
    "install": _plugin_install_handler,
    "uninstall": _plugin_uninstall_handler,
    "enable": _plugin_enable_handler,
    "disable": _plugin_disable_handler,
    "marketplace": _plugin_marketplace_handler,
}


# ---------------------------------------------------------------------------
# Registration tables
# ---------------------------------------------------------------------------


_HANDLERS: dict[
    str, Any,
] = {
    "/help": _cmd_help,
    "/status": _cmd_status,
    "/memory": _cmd_memory,
    "/axioms": _cmd_axioms,
    "/recall": _cmd_recall,
    "/cron": _cmd_cron,
    "/subagent": _cmd_subagent,
    "/bind": _cmd_bind,
    "/unbind": _cmd_unbind,
    "/wechat": _cmd_wechat,
    "/plugin": _cmd_plugin,
    "/theme": _cmd_theme,
    "/exit": _cmd_exit,
}


def list_slash_commands() -> list[str]:
    """Public view of the registered slash commands, sorted.

    Used by the TUI's :class:`pip_agent.tui.history_input.HistoryInput`
    to seed an inline ``Suggester`` so typing ``/m`` surfaces ``/memory``.
    Sorted for stable suggestion order across boots.
    """
    return sorted(_HANDLERS.keys())

# Commands that are only valid at the local CLI. Remote channels
# (WeCom / WeChat / ...) get a terse refusal AND the ``/help`` text
# they see simply doesn't advertise these — remote peers never even
# learn the commands exist.
#
# ``/wechat`` is CLI-only on purpose: scanning a QR code from a WeCom
# or WeChat peer is neither possible nor desirable (you'd be handing
# the scan URL to whoever said ``/wechat add``). The same peer can
# still use ``/bind`` and ``/unbind`` for routing-only changes.
_CLI_ONLY_COMMANDS = {"/subagent", "/exit", "/wechat"}
