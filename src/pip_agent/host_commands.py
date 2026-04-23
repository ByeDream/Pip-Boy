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
* Commands are **flat**. No subcommands except ``/admin`` (which keeps
  ``grant|revoke|list`` inline because its surface was already minimal
  and would be awkward as separate commands).
* A handler returns a :class:`CommandResult`. ``handled=True`` stops
  further processing of the inbound; ``handled=False`` means "this
  wasn't a command I recognize, pass it on to the agent".
* **ACL gates** are owned here, not by individual handlers:

  - ``/help`` and ``/status`` are open to everyone.
  - ``/admin`` is owner-only.
  - Everything else requires owner OR admin.
  - CLI is always owner (there's only one person at a local TTY; any
    further gate is theater).
  - No memory store? Fail-open — we're pre-boot, nothing to protect.

* Unknown ``/foo`` is **NOT handled**. The caller forwards it to the
  agent, which can treat it as free-form text. That preserves the old
  "LLM can interpret slash-commands it doesn't recognize" escape valve.

Out of scope (intentional omissions for v0.4.0)
-----------------------------------------------
* ``/scheduler`` / ``/lanes`` / ``/heartbeat`` / ``/trigger`` /
  ``/cron-trigger`` — surfaces for subsystems the host-rewrite
  stripped. Scheduler health is now visible via WARNING-level logs
  (coalesce misses, auto-disabled cron jobs); no chat surface needed.
* ``/profiles`` / ``/cooldowns`` / ``/stats`` / ``/simulate-failure`` /
  ``/fallback`` — the resilience runner was removed; CC owns retries.
* ``/update`` — out-of-band upgrade flow isn't re-designed yet.
* ``/clean`` — ``/agent delete <id> --yes`` covers the narrow safe
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
    from pip_agent.memory import MemoryStore

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
    invalidate_agent: Callable[[str], None] | None = None
    """Host hook to drop an agent's cached services + session rows.

    Called by lifecycle commands (``delete``, ``archive``, ``reset``)
    after the on-disk state has been mutated, so the host stops holding
    a ``MemoryStore`` that points at wiped / relocated paths. Without
    this, the cached store's next ``save_state`` (or any ``atomic_write``
    in the write path) resurrects ``.pip/`` with a stale ``state.json``
    after the agent was supposed to be gone.
    """


@dataclass(slots=True)
class CommandResult:
    handled: bool
    response: str | None = None


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------


# ``@alice /help`` — WeCom / WeChat mention prefixes. Strip so the
# slash detection below doesn't miss the command. Only leading mentions
# are stripped; a ``@user`` mid-argument is passed through unchanged.
_AT_MENTION_RE = re.compile(r"^(?:@\S*\s+)+")


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

    Returns ``CommandResult(handled=False)`` if the text isn't a
    recognised command — the caller should route it to the agent.
    """
    raw = ctx.inbound.text
    if not isinstance(raw, str):
        return CommandResult(handled=False)

    text = _AT_MENTION_RE.sub("", raw.strip()).strip()
    if not text.startswith("/"):
        return CommandResult(handled=False)

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
    # Policy (plan M9):
    #   * CLI is always owner.  The only way to reach the CLI prompt is
    #     to have shell access on the host, so there is no useful threat
    #     model where we would want to deny it.  This holds even when
    #     ``memory_store`` is ``None`` (tests, early boot, etc.).
    #   * Non-CLI channels fail **closed** when ``memory_store`` is
    #     missing.  Without the store we cannot evaluate ``owner.md`` or
    #     the admin flag on a user profile, and silently treating a
    #     remote sender as "owner" would bypass the whole ACL surface.
    #     Tests that need to exercise remote ACL paths must supply a
    #     memory store (real or fake).
    ch, sid = ctx.inbound.channel, ctx.inbound.sender_id
    ms = ctx.memory_store
    if ch == "cli":
        owner = True
    elif ms is None:
        owner = False
    else:
        owner = ms.is_owner(ch, sid)

    if cmd not in _OPEN_COMMANDS:
        if cmd in _OWNER_ONLY_COMMANDS and not owner:
            return CommandResult(
                handled=True, response="Permission denied: owner only.",
            )
        if not owner:
            # ``is_admin`` needs the memory store for the same reason
            # ``is_owner`` does. Fail closed when it is unavailable.
            admin = ms.is_admin(ch, sid) if ms is not None else False
            if not admin:
                return CommandResult(
                    handled=True,
                    response="Permission denied: admin privileges required.",
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


_HELP_TEXT = """\
Available commands:

  /help                         Show this help
  /status                       Show current agent / session / binding
  /memory                       Memory statistics for the current agent
  /axioms                       Current judgment principles
  /recall <query>               Search stored memories
  /cron                         List scheduled cron jobs
  /admin grant|revoke|list      Manage admin privileges (owner only)
  /exit                         Quit Pip-Boy (CLI only)

  /home                         Leave the current sub-agent and return
                                to pip-boy (clears this chat's binding)

  /agent                        pip-boy only: show pip-boy detail + memory
  /agent list                   pip-boy only: list known agents
  /agent create <id>            pip-boy only, owner: create a new sub-agent
  /agent archive <id>           pip-boy only, owner: move <id>/.pip/ to
                                .pip/archived/ (project files untouched)
  /agent delete <id> --yes      pip-boy only, owner: wipe <id>/.pip/
                                (project files untouched)
  /agent switch <id>            pip-boy only: route this chat to <id>
                                (to come back, use /home)
  /agent reset <id>             pip-boy only, owner: rebuild <id>'s .pip/
                                from a minimal backup — persona.md and
                                HEARTBEAT.md are preserved, the root's
                                workspace-shared state (owner.md,
                                bindings.json, agents_registry.json,
                                credentials/, archived/) is preserved,
                                everything else is wiped and re-created.

Per-agent settings (model, dm_scope, description) are configured via
  <workspace>/<id>/.pip/persona.md
  <workspace>/.pip/agents_registry.json
  <workspace>/.pip/bindings.json
There are no command-line flags for these: edit the file if you need
to deviate from the defaults.

Permissions:
  Owner (CLI or an identity listed in owner.md) can use all commands.
  Admin users can read everything and /agent switch, but not
  create/archive/delete/reset or /admin.
  Others are locked out.

Bindings:
  /agent switch in a group chat creates a guild-level binding; in a
  private chat, a peer-level binding. Bindings persist across
  restarts in <workspace>/.pip/bindings.json. /home removes the
  binding for the current chat so routing falls back to pip-boy."""


def _cmd_help(_ctx: CommandContext, _args: str) -> CommandResult:
    return CommandResult(handled=True, response=_HELP_TEXT)


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

    lines = [
        f"Agent: {agent.name or agent.id} ({agent.id})",
        f"Model: {effective.effective_model}",
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
        return CommandResult(handled=True, response="No cron jobs configured.")

    lines = [f"Cron jobs ({len(jobs)}):"]
    for j in jobs:
        enabled = "on " if j.get("enabled", True) else "off"
        errors = j.get("consecutive_errors", 0)
        kind = j.get("schedule_kind", "?")
        name = j.get("name") or j.get("id") or "?"
        next_at = j.get("next_fire_at")
        if next_at:
            t = datetime.fromtimestamp(float(next_at), tz=UTC)
            next_str = t.strftime("%Y-%m-%d %H:%M UTC")
        else:
            next_str = "n/a"
        lines.append(
            f"  [{enabled}] {name} ({kind}) next={next_str} "
            f"errors={errors}"
        )
    return CommandResult(handled=True, response="\n".join(lines))


# ---------------------------------------------------------------------------
# /agent — pip-boy's management console; /home — leave a sub-agent
# ---------------------------------------------------------------------------
#
# Design principles (agreed in the identity-redesign thread):
#   * ``/agent`` is **pip-boy exclusive**: the whole subcommand family
#     is only accessible when the current chat is bound to the root
#     (``pip-boy``). Sub-agents focus on their own work; they don't
#     manage siblings. To go back to pip-boy, use ``/home``.
#   * Subcommand style (``git``-like), NOT ``--flag`` style.
#   * Exactly one verb per action. No ``/bind`` vs ``/switch`` duplication.
#   * Zero CLI options beyond the subcommand + id. Per-agent tweaks
#     (model, dm_scope, description, binding scope) live in
#     ``persona.md`` / ``agents_registry.json`` / ``bindings.json`` —
#     edit the file if you want to deviate.
#   * archive/delete operate on the agent *identity surface* only
#     (``.pip/``); project files in the sub-agent's cwd are never
#     touched (see :meth:`AgentRegistry.remove_agent`).
#   * ``/agent reset <id>`` preserves identity (``persona.md`` +
#     ``HEARTBEAT.md``) and the root's workspace-shared state
#     (``owner.md``, ``bindings.json``, ``agents_registry.json``,
#     ``credentials/``, ``archived/``); everything else in the
#     agent's ``.pip/`` is wiped and left to be lazily re-created.


def _persist_agent_md(cfg: AgentConfig, pip_dir: Path | None) -> None:
    """Write an AgentConfig to ``<pip_dir>/persona.md``."""
    if not pip_dir:
        return
    pip_dir.mkdir(parents=True, exist_ok=True)
    md_path = pip_dir / "persona.md"

    lines = ["---", f"name: {cfg.name}"]
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
    ``/agent delete`` by default, so a freshly recreated agent at the
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


_AGENT_OWNER_ONLY_SUBCOMMANDS = {"create", "archive", "delete", "reset"}


def _is_owner(ctx: CommandContext) -> bool:
    """Match the ACL rules in :func:`dispatch_command`."""
    ch, sid = ctx.inbound.channel, ctx.inbound.sender_id
    if ch == "cli":
        return True
    ms = ctx.memory_store
    return bool(ms and ms.is_owner(ch, sid))


def _cmd_agent(ctx: CommandContext, args: str) -> CommandResult:
    """Dispatcher for the ``/agent`` subcommand family — pip-boy only.

    Subcommands:

    * ``/agent``                   — show pip-boy detail + memory summary
    * ``/agent list``              — list all known agents
    * ``/agent create <id>``       — materialise ``<workspace>/<id>/.pip/``
    * ``/agent archive <id>``      — move ``<id>/.pip/`` to ``.pip/archived/``
    * ``/agent delete <id> --yes`` — rmtree ``<id>/.pip/`` (project files kept)
    * ``/agent switch <id>``       — route this chat to sub-agent ``<id>``
    * ``/agent reset <id>``        — factory-reset ``<id>``'s memory
                                      (identity preserved; see helper below)

    Pip-boy gating
    --------------
    The whole family is **only usable when the current chat is bound
    to pip-boy**. From a sub-agent, ``/agent`` returns a polite
    redirect to ``/home`` — sub-agents don't manage siblings.

    Owner gating
    ------------
    Read-only ops (``list``, ``switch``, bare ``/agent``) ride the
    top-level "owner-or-admin" gate from :func:`dispatch_command`.
    Destructive ops (``create``, ``archive``, ``delete``, ``reset``)
    re-check owner here.
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
                f"`/agent` is only available from {root_id}. "
                f"You are currently on `{current_id}`. "
                "Run `/home` to return to pip-boy first."
            ),
        )

    if not tokens:
        return _agent_show(ctx)

    sub = tokens[0].lower()
    tail = tokens[1:]
    handler = _AGENT_SUBCOMMANDS.get(sub)
    if handler is None:
        from difflib import get_close_matches
        hint = get_close_matches(sub, _AGENT_SUBCOMMANDS.keys(), n=1, cutoff=0.6)
        suffix = f" Did you mean `/agent {hint[0]}`?" if hint else ""
        return CommandResult(
            handled=True,
            response=(
                f"Unknown /agent subcommand '{sub}'.{suffix}\n"
                "Valid: list, create, archive, delete, switch, reset. "
                "Run `/help` for full usage."
            ),
        )

    if sub in _AGENT_OWNER_ONLY_SUBCOMMANDS and not _is_owner(ctx):
        return CommandResult(
            handled=True,
            response=f"Permission denied: `/agent {sub}` is owner only.",
        )
    return handler(ctx, tail)


def _agent_show(ctx: CommandContext) -> CommandResult:
    """``/agent`` — current agent detail + memory summary."""
    aid = _resolved_agent_id(ctx)
    agent = ctx.registry.get_agent(aid) or ctx.registry.default_agent()
    meta = ctx.registry.metadata_for(agent.id)
    paths = ctx.registry.paths_for(agent.id)

    lines = [
        f"Agent: {agent.name or agent.id} ({agent.id})",
        f"Kind:  {meta.get('kind', 'sub')}",
    ]
    if paths is not None:
        lines.append(f"Cwd:   {paths.cwd}")
    desc = meta.get("description", "")
    if desc:
        lines.append(f"Description: {desc}")
    lines.append(f"Model: {agent.model or '(default)'}")
    lines.append(f"Scope: {agent.dm_scope or '(default)'}")

    ms = ctx.memory_store
    if ms is not None and ms.agent_id == agent.id:
        s = ms.stats()
        lines.append("")
        lines.append("Memory:")
        lines.append(f"  observations: {s['observations']}")
        lines.append(f"  memories:     {s['memories']}")
        lines.append(
            f"  axioms:       {s['axiom_lines']} lines "
            f"({'yes' if s['has_axioms'] else 'none'})"
        )
        for key, label in (
            ("last_reflect_at", "  last reflect: "),
            ("last_consolidate_at", "  last consolidate: "),
        ):
            ts = s.get(key)
            if ts:
                t = datetime.fromtimestamp(float(ts), tz=UTC)
                lines.append(f"{label}{t.strftime('%Y-%m-%d %H:%M UTC')}")

    return CommandResult(handled=True, response="\n".join(lines))


def _agent_list(ctx: CommandContext, _tail: list[str]) -> CommandResult:
    agents = ctx.registry.list_agents()
    if not agents:
        return CommandResult(handled=True, response="(no agents registered)")

    bound_id = _resolved_agent_id(ctx)
    default_id = ctx.registry.default_agent().id

    lines = ["Agents:"]
    for cfg in sorted(agents, key=lambda a: (a.id != default_id, a.id)):
        meta = ctx.registry.metadata_for(cfg.id)
        kind = meta.get("kind", "sub")
        marker = " *" if cfg.id == bound_id else ""
        desc = meta.get("description", "")
        descr = f" — {desc}" if desc else ""
        lines.append(
            f"  [{kind}] {cfg.id}{marker} ({cfg.name or cfg.id}){descr}"
        )
    lines.append("\n* = currently routed for this chat. "
                 "Use `/agent switch <id>` to change.")
    return CommandResult(handled=True, response="\n".join(lines))


def _agent_create(ctx: CommandContext, tail: list[str]) -> CommandResult:
    if not tail:
        return CommandResult(
            handled=True, response="Usage: /agent create <id>",
        )
    # We deliberately accept no description / no flags — edit
    # ``agents_registry.json`` by hand if you want a description, or
    # tweak ``<id>/.pip/persona.md`` for model/scope. The command line
    # stays single-purpose.
    raw_id = tail[0]
    if len(tail) > 1:
        return CommandResult(
            handled=True,
            response=(
                "Usage: /agent create <id>\n"
                "(No options. Edit <id>/.pip/persona.md or "
                "agents_registry.json for model / scope / description.)"
            ),
        )

    agent_id = normalize_agent_id(raw_id)
    if ctx.registry.get_agent(agent_id) is not None:
        return CommandResult(
            handled=True, response=f"Agent '{agent_id}' already exists.",
        )
    cfg, err = _create_agent_on_disk(ctx.registry, agent_id)
    if err or cfg is None:
        return CommandResult(
            handled=True,
            response=err or f"Failed to create agent '{agent_id}'.",
        )
    paths = ctx.registry.paths_for(cfg.id)
    loc = f" at {paths.cwd}" if paths is not None else ""
    return CommandResult(
        handled=True,
        response=(
            f"Created agent '{agent_id}'{loc}.\n"
            f"Use `/agent switch {agent_id}` to route this chat to it."
        ),
    )


_SUB_AGENT_IDENTITY_TEMPLATE = """\
# Identity

You are {agent_id}, a personal assistant sub-agent of Pip-Boy, powered by {{model_name}}.
You are a coding agent working in {{workdir}} that helps the USER with software engineering tasks.
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
    registry: AgentRegistry, agent_id: str,
) -> tuple[AgentConfig | None, str | None]:
    """Materialise a new sub-agent directory + registry entry.

    The new agent inherits the default (``pip-boy``) agent's **full**
    persona body — Core Philosophy, System Communication, Tone,
    Identity Recognition, Tool Calling, Memory guidance, etc. — so
    it actually knows how to interpret the ``# User`` block that
    :meth:`MemoryStore.enrich_prompt` injects at prompt time.

    Only the ``# Identity`` section is rewritten, to name the new
    agent and flag the shared-owner relationship with Pip-Boy.
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
    new_identity = _SUB_AGENT_IDENTITY_TEMPLATE.format(agent_id=agent_id)
    body = _replace_identity_section(default.system_body or "", new_identity)
    cfg = replace(default, id=agent_id, name=agent_id, system_body=body)

    registry.register_agent(cfg)
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
            handled=True, response="Usage: /agent archive <id>",
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
        log.exception("save_registry failed after /agent archive")

    cc_note = (
        f"\nAlso purged CC project dir: {cc_removed}." if cc_removed else ""
    )
    if dest is None:
        return CommandResult(
            handled=True,
            response=f"Archived agent '{agent_id}' (no .pip/ on disk).{cc_note}",
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
    """``/agent delete <id> --yes`` — purge identity surface only."""
    if not tail:
        return CommandResult(
            handled=True, response="Usage: /agent delete <id> --yes",
        )
    confirmed = "--yes" in tail
    positional = [t for t in tail if not t.startswith("--")]
    if not positional:
        return CommandResult(
            handled=True, response="Usage: /agent delete <id> --yes",
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
                "kept. Use `/agent archive {id}` for a reversible move."
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
        log.exception("save_registry failed after /agent delete")

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


def _agent_switch(ctx: CommandContext, tail: list[str]) -> CommandResult:
    """``/agent switch <id>`` — route this chat to sub-agent ``<id>``.

    Switching *back* to pip-boy is handled by the separate ``/home``
    command, not here. ``/agent switch pip-boy`` is rejected with a
    redirect, so there's exactly one idiom for each direction:

        pip-boy → sub-agent : /agent switch <id>
        sub-agent → pip-boy : /home
    """
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
                "Usage: /agent switch <id>\n"
                f"Known sub-agents: {known}"
            ),
        )

    agent_id = normalize_agent_id(tail[0])
    default_id = ctx.registry.default_agent().id
    if agent_id == default_id:
        return CommandResult(
            handled=True,
            response=(
                f"`/agent switch {default_id}` is not supported. "
                "You are already on pip-boy; use `/home` from a "
                "sub-agent to return here."
            ),
        )
    agent = ctx.registry.get_agent(agent_id)
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
                f"Unknown agent '{agent_id}'.\n"
                f"Known sub-agents: {known}\n"
                f"Use `/agent create {agent_id}` to make one first."
            ),
        )

    inbound = ctx.inbound
    if inbound.is_group:
        if not inbound.guild_id:
            return CommandResult(
                handled=True,
                response="Cannot switch in group: missing guild_id.",
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
        response=f"Switched to {agent.name or agent.id} ({agent_id}).",
    )


# ---------------------------------------------------------------------------
# /agent reset — backup · delete · rebuild · restore
# ---------------------------------------------------------------------------
#
# What counts as "identity" (always preserved, copied into the rebuilt
# .pip/):
_RESET_PRESERVE_FILES = ("persona.md", "HEARTBEAT.md")

# What counts as "root-only workspace-shared state". For a sub-agent
# reset these are no-ops (they don't live in the sub-agent's .pip/).
# For pip-boy reset they're preserved so ACL, routing, channel
# credentials, and the archive trail survive a reset.
_RESET_PRESERVE_ROOT_FILES = (
    "owner.md",
    "bindings.json",
    "agents_registry.json",
)
_RESET_PRESERVE_ROOT_DIRS = ("credentials", "archived")


def _agent_reset(ctx: CommandContext, tail: list[str]) -> CommandResult:
    """``/agent reset <id>`` — rebuild ``<id>``'s .pip/ from a minimal backup.

    Algorithm (per the design note in the identity-redesign thread):

        1. Stash the "identity" files (persona.md, HEARTBEAT.md) and,
           for the root agent, the workspace-shared state
           (owner.md, bindings.json, agents_registry.json,
           credentials/, archived/) to a sibling temp directory.
        2. Delete the agent's entire .pip/ directory.
        3. Recreate an empty .pip/ and restore the stash into it.
        4. Remove the temp stash.

    Outcome: persona + identity preserved, memory layer and any
    other bookkeeping files (observations, memories.json, axioms.md,
    state.json, users/, incoming/, cron.json, sdk_sessions entries
    for this agent, .scaffold_manifest.json, ...) wiped and left to
    be lazily re-created by the running host.

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
                "Usage: /agent reset <id>\n"
                f"Known agents: {ids}"
            ),
        )

    agent_id = normalize_agent_id(tail[0])
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
    is_root = agent_id == ctx.registry.default_agent().id

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
    preserve_dirs: list[str] = []
    if is_root:
        preserve_files.extend(_RESET_PRESERVE_ROOT_FILES)
        preserve_dirs.extend(_RESET_PRESERVE_ROOT_DIRS)

    # --- 1. Stash ---------------------------------------------------
    stash = Path(
        tempfile.mkdtemp(prefix=f"pip-reset-{agent_id}-", dir=pip_dir.parent)
    )
    try:
        for name in preserve_files:
            src = pip_dir / name
            if src.is_file():
                shutil.copy2(src, stash / name)
        for dname in preserve_dirs:
            src = pip_dir / dname
            if src.is_dir():
                shutil.copytree(src, stash / dname)

        # --- 2. Delete --------------------------------------------
        shutil.rmtree(pip_dir)

        # --- 3. Rebuild + restore --------------------------------
        pip_dir.mkdir(parents=True)
        # Re-seed the standard MemoryStore subdirs so the rebuilt
        # ``.pip/`` matches what a fresh ``MemoryStore.__init__``
        # produces. Without this, any cached per-agent service
        # (AgentHost._agents) keeps a MemoryStore whose directories
        # no longer exist, and the first reflect after reset dies
        # with ENOENT on ``observations/<date>.jsonl``.
        (pip_dir / "observations").mkdir(exist_ok=True)
        (pip_dir / "users").mkdir(exist_ok=True)
        for name in preserve_files:
            staged = stash / name
            if staged.is_file():
                shutil.copy2(staged, pip_dir / name)
        for dname in preserve_dirs:
            staged = stash / dname
            if staged.is_dir():
                shutil.copytree(staged, pip_dir / dname)
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

    preserved_desc = "persona.md, HEARTBEAT.md"
    if is_root:
        preserved_desc += (
            ", owner.md, bindings.json, agents_registry.json, "
            "credentials/, archived/"
        )
    cc_note = (
        f"\nAlso purged CC project dir: {cc_removed}." if cc_removed else ""
    )
    return CommandResult(
        handled=True,
        response=(
            f"Reset agent '{agent_id}'. Preserved: {preserved_desc}. "
            "Memory (observations, memories, axioms, state) and "
            "per-agent bookkeeping were wiped."
            f"{cc_note}"
        ),
    )


_AGENT_SUBCOMMANDS: dict[str, Any] = {
    "list": _agent_list,
    "create": _agent_create,
    "archive": _agent_archive,
    "delete": _agent_delete,
    "switch": _agent_switch,
    "reset": _agent_reset,
}


# ---------------------------------------------------------------------------
# /home — the counterpart of ``/agent switch``, always returns to pip-boy
# ---------------------------------------------------------------------------


def _cmd_home(ctx: CommandContext, _args: str) -> CommandResult:
    """``/home`` — leave the current sub-agent and return to pip-boy.

    Removes the binding row that is currently routing this chat to
    a sub-agent. Routing falls back to the default agent (pip-boy)
    via the normal resolver fallback. Running ``/home`` while
    already on pip-boy is a friendly no-op so the command is safe
    to hit repeatedly.
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
                response="Cannot /home in group: missing guild_id.",
            )
        match_key, match_value = "guild_id", inbound.guild_id
    else:
        match_key, match_value = "peer_id", inbound.peer_id

    removed = ctx.bindings.remove(match_key, match_value)
    if removed:
        try:
            ctx.bindings.save(ctx.bindings_path)
        except Exception:
            log.exception("Failed to persist bindings after /home")
    return CommandResult(
        handled=True,
        response=f"Back to {root_id}. Binding cleared.",
    )


# ---------------------------------------------------------------------------
# /admin
# ---------------------------------------------------------------------------


def _cmd_admin(ctx: CommandContext, args: str) -> CommandResult:
    """Manage admin privileges (owner only)."""
    ms = ctx.memory_store
    if not ms:
        return CommandResult(handled=True, response="Memory store unavailable.")

    parts = args.strip().split(None, 1)
    if not parts:
        return CommandResult(
            handled=True, response="Usage: /admin grant|revoke|list [name]",
        )

    sub = parts[0].lower()
    name = parts[1].strip() if len(parts) > 1 else ""

    if sub == "list":
        admins = ms.list_admins()
        if not admins:
            return CommandResult(handled=True, response="No admin users.")
        return CommandResult(
            handled=True,
            response="Admin users:\n" + "\n".join(f"  - {a}" for a in admins),
        )
    if sub in ("grant", "revoke"):
        if not name:
            return CommandResult(
                handled=True, response=f"Usage: /admin {sub} <name>",
            )
        result = ms.set_admin(name, grant=(sub == "grant"))
        return CommandResult(handled=True, response=result)
    return CommandResult(
        handled=True,
        response="Usage: /admin grant|revoke <name> | /admin list",
    )


# ---------------------------------------------------------------------------
# (Legacy handlers /agents, /create-agent, /archive-agent, /delete-agent,
# /switch, /bind, /unbind, /reset removed — functionality consolidated
# into `/agent <subcommand>` above.)
# ---------------------------------------------------------------------------


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
    "/admin": _cmd_admin,
    "/agent": _cmd_agent,
    "/home": _cmd_home,
    "/exit": _cmd_exit,
}

_OPEN_COMMANDS = {"/help", "/status"}
# ``/agent create`` / ``archive`` / ``delete`` / ``reset`` are owner-only,
# but since they're all subcommands of a single top-level ``/agent``,
# we gate per-subcommand inside the dispatcher rather than at this
# top-level ACL table. Pure-read ``/agent`` and ``/agent list`` /
# ``/agent switch`` remain owner-or-admin (the default).
_OWNER_ONLY_COMMANDS = {"/admin"}
