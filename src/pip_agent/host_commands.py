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
* ``/clean`` — ``/unbind`` plus a manual FS delete covers it without
  the chat-as-root-shell footgun.
"""

from __future__ import annotations

import logging
import re
import shlex
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
        return CommandResult(handled=False)

    # --- ACL gate ---
    ch, sid = ctx.inbound.channel, ctx.inbound.sender_id
    ms = ctx.memory_store
    owner = ms.is_owner(ch, sid) if ms else (ch == "cli")

    if cmd not in _OPEN_COMMANDS:
        if cmd in _OWNER_ONLY_COMMANDS and not owner:
            return CommandResult(
                handled=True, response="Permission denied: owner only.",
            )
        if not owner:
            admin = ms.is_admin(ch, sid) if ms else False
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

  /help                          Show this help
  /status                        Show current agent / session / binding
  /memory                        Memory statistics for the current agent
  /axioms                        Current judgment principles
  /recall <query>                Search stored memories
  /cron                          List scheduled cron jobs
  /bind <agent-id> [options]     Bind current chat to an agent
  /unbind                        Remove current chat's binding
  /name <display_name>           Rename the current agent
  /reset                         Factory-reset memory (keep binding + persona)
  /admin grant|revoke|list       Manage admin privileges (owner only)
  /exit                          Quit Pip-Boy (CLI only)

/bind options:
  --scope <dm_scope>             Session isolation (per-guild|per-guild-peer|main)
  --model <model>                Override model for this binding

Permissions:
  Owner (CLI or an identity listed in owner.md) can use all commands.
  Admin users can use everything except /admin. Others are locked out.

Bindings:
  In a group chat, /bind creates a guild-level binding.
  In a private chat, /bind creates a peer-level binding.
  Bindings persist across restarts in .pip/agents/bindings.json."""


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
# /bind, /unbind, /name
# ---------------------------------------------------------------------------


_BIND_KNOWN_FLAGS = {"scope", "model"}


def _persist_agent_md(cfg: AgentConfig, agents_dir: Path | None) -> None:
    """Write an AgentConfig to its persona.md inside the agent subdir.

    Mirrors legacy behaviour, trimmed to the fields the slimmed-down
    :class:`AgentConfig` still carries (name, model, dm_scope, body).
    No-op if the registry wasn't started from a real directory.
    """
    if not agents_dir:
        return
    subdir = agents_dir / cfg.id
    subdir.mkdir(parents=True, exist_ok=True)
    md_path = subdir / "persona.md"

    lines = ["---", f"name: {cfg.name}"]
    if cfg.model:
        lines.append(f"model: {cfg.model}")
    if cfg.dm_scope:
        lines.append(f"dm_scope: {cfg.dm_scope}")
    lines.append("---\n")
    frontmatter = "\n".join(lines)
    body = cfg.system_body or ""
    md_path.write_text(frontmatter + body + "\n", encoding="utf-8")


def _auto_create_agent(
    registry: AgentRegistry, agent_id: str,
) -> tuple[AgentConfig | None, str | None]:
    """Clone the default agent into ``agent_id`` and persist to disk."""
    import shutil
    from dataclasses import replace

    agents_dir = registry.agents_dir
    if not agents_dir:
        return (
            None,
            f"Agent '{agent_id}' not found and agents directory is "
            "not configured.",
        )

    default = registry.default_agent()
    body = (
        "## Identity\n\n"
        f"You are {agent_id}, a personal assistant agent.\n"
        "Your working directory is {workdir}.\n"
        "If AGENTS.md exists in your working directory, read it for "
        "project context."
    )
    cfg = replace(default, id=agent_id, name=agent_id, system_body=body)
    _persist_agent_md(cfg, agents_dir)

    # Copy HEARTBEAT.md from the default agent so the new one fires
    # heartbeats with a sensible starting template rather than silence.
    default_hb = agents_dir / default.id / "HEARTBEAT.md"
    new_hb = agents_dir / agent_id / "HEARTBEAT.md"
    if default_hb.is_file() and not new_hb.exists():
        new_hb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(default_hb, new_hb)

    registry.register_agent(cfg)
    return cfg, None


def _cmd_bind(ctx: CommandContext, args: str) -> CommandResult:
    if not args.strip():
        return CommandResult(
            handled=True,
            response="Usage: /bind <agent-id> [--scope s] [--model m]",
        )

    try:
        tokens = shlex.split(args)
    except ValueError as exc:
        return CommandResult(handled=True, response=f"Parse error: {exc}")

    agent_id_raw = tokens[0]
    agent_id = normalize_agent_id(agent_id_raw)
    agent = ctx.registry.get_agent(agent_id)
    created_new = False
    if agent is None:
        agent, err = _auto_create_agent(ctx.registry, agent_id)
        if err:
            return CommandResult(handled=True, response=err)
        created_new = True

    # Parse --flag value pairs.
    overrides: dict[str, Any] = {}
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--"):
            return CommandResult(handled=True, response=f"Unknown option: {tok}")
        key = tok.lstrip("-").replace("-", "_")
        if key not in _BIND_KNOWN_FLAGS:
            return CommandResult(handled=True, response=f"Unknown option: {tok}")
        if i + 1 >= len(tokens):
            return CommandResult(handled=True, response=f"Missing value for {tok}")
        overrides[key] = tokens[i + 1]
        i += 2

    inbound = ctx.inbound
    if inbound.is_group:
        if not inbound.guild_id:
            return CommandResult(
                handled=True,
                response="Cannot bind in group: missing guild_id.",
            )
        tier, match_key, match_value = 2, "guild_id", inbound.guild_id
    else:
        tier, match_key, match_value = 1, "peer_id", inbound.peer_id

    # ``remove`` + ``add`` is deliberate: /bind replaces any existing
    # binding at the same (key, value) rather than stacking.
    ctx.bindings.remove(match_key, match_value)
    binding = Binding(
        agent_id=agent_id,
        tier=tier,
        match_key=match_key,
        match_value=match_value,
        overrides=overrides,
    )
    ctx.bindings.add(binding)
    ctx.bindings.save(ctx.bindings_path)

    effective = resolve_effective_config(agent, binding)
    lines: list[str] = []
    if created_new:
        agents_dir = ctx.registry.agents_dir
        lines.append(f"Created new agent '{agent_id}' (cloned from default)")
        if agents_dir:
            lines.append(f"  config: {agents_dir / agent_id / 'persona.md'}")
    lines.extend([
        f"Bound to {agent.name or agent.id} ({agent_id})",
        f"  scope: {effective.effective_dm_scope} | "
        f"model: {effective.effective_model}",
        f"  binding: {binding.display()}",
    ])
    return CommandResult(handled=True, response="\n".join(lines))


def _cmd_unbind(ctx: CommandContext, _args: str) -> CommandResult:
    inbound = ctx.inbound
    if inbound.is_group and inbound.guild_id:
        removed = ctx.bindings.remove("guild_id", inbound.guild_id)
    else:
        removed = ctx.bindings.remove("peer_id", inbound.peer_id)

    if removed:
        ctx.bindings.save(ctx.bindings_path)
        return CommandResult(
            handled=True,
            response="Binding removed. Falling back to default agent.",
        )
    return CommandResult(handled=True, response="No binding found for this context.")


def _cmd_name(ctx: CommandContext, args: str) -> CommandResult:
    new_name = args.strip()
    if not new_name:
        return CommandResult(handled=True, response="Usage: /name <display_name>")

    from dataclasses import replace

    inbound = ctx.inbound
    agent_id, _ = ctx.bindings.resolve(
        channel=inbound.channel,
        account_id=inbound.account_id,
        guild_id=inbound.guild_id,
        peer_id=inbound.peer_id,
    )
    if not agent_id:
        agent_id = ctx.registry.default_agent().id

    agent = ctx.registry.get_agent(agent_id)
    if not agent:
        return CommandResult(handled=True, response="No agent found for this context.")

    agent = replace(agent, name=new_name)
    ctx.registry.register_agent(agent)
    _persist_agent_md(agent, ctx.registry.agents_dir)
    return CommandResult(
        handled=True,
        response=f"Agent '{agent_id}' renamed to {new_name}.",
    )


# ---------------------------------------------------------------------------
# /reset
# ---------------------------------------------------------------------------


def _cmd_reset(ctx: CommandContext, _args: str) -> CommandResult:
    """Factory-reset memory for the routed agent; keep binding and persona."""
    from pip_agent.memory import MemoryStore

    inbound = ctx.inbound
    agent_id, binding = ctx.bindings.resolve(
        channel=inbound.channel,
        account_id=inbound.account_id,
        guild_id=inbound.guild_id,
        peer_id=inbound.peer_id,
    )
    if not agent_id:
        agent_id = ctx.registry.default_agent().id

    agents_dir = ctx.registry.agents_dir
    if not agents_dir:
        return CommandResult(
            handled=True,
            response="[error] agents directory not configured.",
        )
    # Build a fresh MemoryStore bound to the routed agent rather than
    # reusing ``ctx.memory_store`` — the latter is tied to the caller's
    # session's agent, which may not be the one the caller wants to
    # reset when using /bind-then-/reset in the same turn.
    store = MemoryStore(base_dir=agents_dir, agent_id=agent_id)
    store.factory_reset()
    return CommandResult(
        handled=True,
        response=(
            f"Memory factory-reset for agent `{agent_id}` "
            "(observations, memories.json, axioms.md, state.json). "
            "Bindings and persona are unchanged."
        ),
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
# /exit — inline-intercepted in agent_host for CLI, but we still need to
# produce a friendly response for non-CLI callers that type ``/exit``.
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
    "/bind": _cmd_bind,
    "/unbind": _cmd_unbind,
    "/name": _cmd_name,
    "/reset": _cmd_reset,
    "/admin": _cmd_admin,
    "/exit": _cmd_exit,
}

_OPEN_COMMANDS = {"/help", "/status"}
_OWNER_ONLY_COMMANDS = {"/admin"}
