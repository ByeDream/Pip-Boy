"""
Unified slash-command dispatch for all channels.

Commands are intercepted before routing and agent_loop. Each handler
receives the current message context and returns a response string
(or None to signal no reply needed).

All commands are flat (single-level):
  /help, /bind, /name, /unbind, /clean, /reset, /status, /admin, /update, /exit
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
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
    from pip_agent.memory import MemoryStore


@dataclass
class CommandContext:
    inbound: InboundMessage
    registry: AgentRegistry
    bindings: BindingTable
    bindings_path: Path
    workdir: str = ""
    memory_store: MemoryStore | None = None


@dataclass
class CommandResult:
    handled: bool
    response: str | None = None
    exit_requested: bool = False


_AT_MENTION_RE = re.compile(r"^(?:@\S*\s+)+")


def dispatch_command(ctx: CommandContext) -> CommandResult:
    """Parse and dispatch a slash command. Returns CommandResult."""
    text = ctx.inbound.text.strip()
    text = _AT_MENTION_RE.sub("", text).strip()
    if not text.startswith("/"):
        return CommandResult(handled=False)

    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    handlers = {
        "/help": _cmd_help,
        "/bind": _cmd_bind,
        "/name": _cmd_name,
        "/unbind": _cmd_unbind,
        "/clean": _cmd_clean,
        "/reset": _cmd_reset,
        "/status": _cmd_status,
        "/memory": _cmd_memory,
        "/axioms": _cmd_axioms,
        "/recall": _cmd_recall,
        "/admin": _cmd_admin,
        "/update": _cmd_update,
        "/exit": _cmd_exit,
    }

    handler = handlers.get(cmd)
    if handler is None:
        return CommandResult(handled=False)

    # --- ACL gate ---
    ms = ctx.memory_store
    ch, sid = ctx.inbound.channel, ctx.inbound.sender_id
    owner = ms.is_owner(ch, sid) if ms else (ch == "cli")

    _OWNER_ONLY: set[str] = {"/admin"}
    if cmd in _OWNER_ONLY and not owner:
        return CommandResult(handled=True, response="Permission denied: owner only.")
    if not owner:
        admin = ms.is_admin(ch, sid) if ms else False
        if not admin:
            return CommandResult(
                handled=True,
                response="Permission denied: admin privileges required.",
            )

    return handler(ctx, args)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
Available commands (require admin or owner privileges):

  /help                          Show this help
  /bind <agent-id> [options]     Bind current chat to an agent (auto-creates if needed)
  /name <display_name>           Set display name for the current agent
  /unbind                        Remove current chat's routing binding
  /clean                         Remove binding and delete the agent + all its data
  /reset                         Factory-reset agent memory (keep binding and persona)
  /status                        Show current routing info
  /memory                        Show memory statistics for the current agent
  /axioms                        Show current judgment principles
  /recall <query>                Search through stored memories
  /admin grant|revoke|list       Manage admin privileges (owner only)
  /update                        Upgrade pip-boy to latest version and restart
  /exit                          Quit Pip-Boy (CLI only)

/bind options:
  --scope <dm_scope>             Session isolation (per-guild, per-guild-peer, main)
  --model <model>                Override model
  --max-tokens <n>               Override max tokens
  --compact-threshold <n>        Override compact threshold
  --compact-micro-age <n>        Override compact micro age

Permissions: owner (CLI or owner.md Identifiers) can use all commands.
Admin users can use all commands except /admin. Others cannot use commands.

In a group chat, /bind creates a guild-level (T2) binding.
In a private chat, /bind creates a peer-level (T1) binding.
Bindings persist across restarts in .pip/agents/bindings.json."""


def _cmd_help(ctx: CommandContext, args: str) -> CommandResult:
    return CommandResult(handled=True, response=_HELP_TEXT)


# ---------------------------------------------------------------------------
# /bind
# ---------------------------------------------------------------------------

def _persist_agent_md(cfg: AgentConfig, agents_dir: Path | None) -> None:
    """Write an AgentConfig to its persona.md inside the agent subdirectory."""
    if not agents_dir:
        return
    agent_subdir = agents_dir / cfg.id
    agent_subdir.mkdir(parents=True, exist_ok=True)
    md_path = agent_subdir / "persona.md"
    frontmatter = (
        f"---\n"
        f"name: {cfg.name}\n"
        f"model: {cfg.effective_model}\n"
        f"max_tokens: {cfg.effective_max_tokens}\n"
        f"dm_scope: {cfg.effective_dm_scope}\n"
        f"compact_threshold: {cfg.effective_compact_threshold}\n"
        f"compact_micro_age: {cfg.effective_compact_micro_age}\n"
        f"---\n"
    )
    body = cfg.system_body or ""
    md_path.write_text(frontmatter + body + "\n", encoding="utf-8")


def _auto_create_agent(
    registry: AgentRegistry, agent_id: str,
) -> tuple[AgentConfig | None, str | None]:
    """Clone the default agent with a new id/name, persist to disk, register."""
    from dataclasses import replace

    agents_dir = registry.agents_dir
    if not agents_dir:
        return None, f"Agent '{agent_id}' not found and agents directory is not configured."

    default = registry.default_agent()
    body = (
        "## Identity\n\n"
        f"You are {agent_id}, a personal assistant agent.\n"
        "Your working directory is {workdir}.\n"
        "If AGENTS.md exists in your working directory, read it for project context."
    )
    cfg = replace(default, id=agent_id, name=agent_id, system_body=body)

    _persist_agent_md(cfg, agents_dir)
    registry.register_agent(cfg)
    return cfg, None


def _cmd_bind(ctx: CommandContext, args: str) -> CommandResult:
    if not args.strip():
        return CommandResult(
            handled=True,
            response="Usage: /bind <agent-id> [options]\nType /help for details.",
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

    _KNOWN_FLAGS = {"scope", "model", "max_tokens", "compact_threshold", "compact_micro_age"}

    overrides: dict[str, Any] = {}
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok.lstrip("-").replace("-", "_")
            if key not in {k.replace("-", "_") for k in _KNOWN_FLAGS}:
                return CommandResult(handled=True, response=f"Unknown option: {tok}")
            if i + 1 >= len(tokens):
                return CommandResult(handled=True, response=f"Missing value for {tok}")
            overrides[key] = tokens[i + 1]
            i += 2
        else:
            return CommandResult(handled=True, response=f"Unknown option: {tok}")

    inbound = ctx.inbound
    if inbound.is_group and inbound.guild_id:
        tier, match_key, match_value = 2, "guild_id", inbound.guild_id
    else:
        tier, match_key, match_value = 1, "peer_id", inbound.peer_id

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
    display_name = agent.name or agent.id
    scope = effective.effective_dm_scope
    model = effective.effective_model

    lines: list[str] = []
    if created_new:
        agents_dir = ctx.registry.agents_dir
        lines.append(f"Created new agent '{agent_id}' (cloned from default)")
        if agents_dir:
            lines.append(f"  config: {agents_dir / agent_id / 'persona.md'}")
    lines.extend([
        f"Bound to **{display_name}** ({agent_id})",
        f"  scope: {scope} | model: {model}",
        f"  binding: {binding.display()}",
    ])
    return CommandResult(handled=True, response="\n".join(lines))


# ---------------------------------------------------------------------------
# /name
# ---------------------------------------------------------------------------

def _cmd_name(ctx: CommandContext, args: str) -> CommandResult:
    new_name = args.strip()
    if not new_name:
        return CommandResult(handled=True, response="Usage: /name <display_name>")

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

    from dataclasses import replace as _replace
    agent = _replace(agent, name=new_name)
    ctx.registry.register_agent(agent)
    _persist_agent_md(agent, ctx.registry.agents_dir)

    return CommandResult(
        handled=True,
        response=f"Agent '{agent_id}' renamed to **{new_name}**.",
    )


# ---------------------------------------------------------------------------
# /unbind
# ---------------------------------------------------------------------------

def _cmd_unbind(ctx: CommandContext, args: str) -> CommandResult:
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


# ---------------------------------------------------------------------------
# /clean
# ---------------------------------------------------------------------------

def _cmd_clean(ctx: CommandContext, args: str) -> CommandResult:
    """Remove current chat's binding and delete its bound agent (except default)."""
    inbound = ctx.inbound
    if inbound.is_group and inbound.guild_id:
        match_key, match_value = "guild_id", inbound.guild_id
    else:
        match_key, match_value = "peer_id", inbound.peer_id

    agent_id, _ = ctx.bindings.resolve(
        channel=inbound.channel,
        account_id=inbound.account_id,
        guild_id=inbound.guild_id,
        peer_id=inbound.peer_id,
    )

    removed_binding = ctx.bindings.remove(match_key, match_value)
    if removed_binding:
        ctx.bindings.save(ctx.bindings_path)

    lines: list[str] = []
    if removed_binding:
        lines.append("Binding removed.")
    else:
        lines.append("No binding found for this context.")

    if agent_id:
        deleted = ctx.registry.remove_agent(agent_id, delete_file=True)
        if deleted:
            lines.append(f"Agent '{agent_id}' deleted.")
        else:
            lines.append(f"Agent '{agent_id}' is the default and cannot be deleted.")

    lines.append("Falling back to default agent.")
    return CommandResult(handled=True, response="\n".join(lines))


# ---------------------------------------------------------------------------
# /reset
# ---------------------------------------------------------------------------

def _resolve_effective_agent(ctx: CommandContext) -> AgentConfig:
    """Resolve the effective agent for the current context."""
    inbound = ctx.inbound
    agent_id, binding = ctx.bindings.resolve(
        channel=inbound.channel,
        account_id=inbound.account_id,
        guild_id=inbound.guild_id,
        peer_id=inbound.peer_id,
    )
    if not agent_id:
        agent_id = ctx.registry.default_agent().id
    agent_cfg = ctx.registry.get_agent(agent_id)
    if not agent_cfg:
        agent_cfg = ctx.registry.default_agent()
    return resolve_effective_config(agent_cfg, binding)


def _cmd_reset(ctx: CommandContext, args: str) -> CommandResult:
    """Factory-reset memory for the routed agent; keep binding and persona."""
    from pip_agent.memory import MemoryStore

    eff = _resolve_effective_agent(ctx)
    agents_dir = ctx.registry.agents_dir
    if not agents_dir:
        return CommandResult(
            handled=True,
            response="[error] agents directory not configured.",
        )
    store = MemoryStore(agents_dir, eff.id)
    store.factory_reset()
    return CommandResult(
        handled=True,
        response=(
            f"Memory factory-reset for agent `{eff.id}` "
            "(observations, memories.json, axioms.md, state.json). "
            "Bindings and persona are unchanged."
        ),
    )


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

def _cmd_status(ctx: CommandContext, args: str) -> CommandResult:
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

    agent = ctx.registry.get_agent(agent_id)
    if not agent:
        agent = ctx.registry.default_agent()

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
    ]
    if binding:
        lines.append(f"Binding: {binding.display()}")
    else:
        lines.append("Binding: (none — using default)")

    if inbound.is_group:
        lines.append(f"Guild: {inbound.guild_id}")
    lines.append(f"Peer: {inbound.peer_id}")

    return CommandResult(handled=True, response="\n".join(lines))


# ---------------------------------------------------------------------------
# /memory, /axioms, /recall
# ---------------------------------------------------------------------------

def _cmd_memory(ctx: CommandContext, args: str) -> CommandResult:
    if ctx.memory_store is None:
        return CommandResult(handled=True, response="Memory system not initialized.")
    s = ctx.memory_store.stats()
    lines = [
        f"Agent: {s['agent_id']}",
        f"Observations: {s['observations']}",
        f"Memories: {s['memories']}",
        f"Axioms: {'yes' if s['has_axioms'] else 'none'} ({s['axiom_lines']} lines)",
    ]
    if s.get("last_reflect_at"):
        from datetime import datetime, timezone
        t = datetime.fromtimestamp(s["last_reflect_at"], tz=timezone.utc)
        lines.append(f"Last reflect: {t.strftime('%Y-%m-%d %H:%M UTC')}")
    if s.get("last_consolidate_at"):
        from datetime import datetime, timezone
        t = datetime.fromtimestamp(s["last_consolidate_at"], tz=timezone.utc)
        lines.append(f"Last consolidate: {t.strftime('%Y-%m-%d %H:%M UTC')}")
    return CommandResult(handled=True, response="\n".join(lines))


def _cmd_axioms(ctx: CommandContext, args: str) -> CommandResult:
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
    lines = [f"- {r.get('text', '')} (score: {r.get('score', 0)})" for r in results]
    return CommandResult(handled=True, response="\n".join(lines))


# ---------------------------------------------------------------------------
# /admin
# ---------------------------------------------------------------------------

def _cmd_admin(ctx: CommandContext, args: str) -> CommandResult:
    """Manage admin privileges (owner only)."""
    parts = args.strip().split(None, 1)
    if not parts:
        return CommandResult(
            handled=True,
            response="Usage: /admin grant|revoke|list [name]",
        )
    sub = parts[0].lower()
    name = parts[1].strip() if len(parts) > 1 else ""

    ms = ctx.memory_store
    if not ms:
        return CommandResult(handled=True, response="Memory store unavailable.")

    if sub == "list":
        admins = ms.list_admins()
        if not admins:
            return CommandResult(handled=True, response="No admin users.")
        return CommandResult(
            handled=True,
            response="Admin users:\n" + "\n".join(f"  - {a}" for a in admins),
        )
    if sub in ("grant", "revoke") and name:
        result = ms.set_admin(name, grant=(sub == "grant"))
        return CommandResult(handled=True, response=result)
    return CommandResult(
        handled=True,
        response="Usage: /admin grant|revoke <name> | /admin list",
    )


# ---------------------------------------------------------------------------
# /update
# ---------------------------------------------------------------------------

def _cmd_update(ctx: CommandContext, args: str) -> CommandResult:
    """Upgrade pip-boy to the latest PyPI version and restart the process."""
    import os
    import subprocess
    import sys

    from pip_agent import __version__ as current_ver

    print(f"  [system] Current version: v{current_ver}")
    print("  [system] Checking for updates...")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip-boy"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        if sys.platform == "win32" and "WinError 32" in result.stderr:
            return _update_via_launcher(current_ver)
        return CommandResult(
            handled=True,
            response=f"Update failed:\n{result.stderr.strip()}",
        )

    from importlib.metadata import version
    new_ver = version("pip-boy")

    if new_ver == current_ver:
        return CommandResult(
            handled=True,
            response=f"Already at latest version (v{current_ver}).",
        )

    print(f"  [system] Updated to v{new_ver}. Restarting...")
    os.execv(sys.executable, [sys.executable, "-m", "pip_agent"] + sys.argv[1:])


def _update_via_launcher(current_ver: str) -> CommandResult:
    """Windows fallback: spawn a helper script that upgrades after we exit."""
    import os
    import subprocess
    import sys
    import tempfile

    python = sys.executable
    pid = os.getpid()
    cwd = os.getcwd()

    script_path = os.path.join(tempfile.gettempdir(), "pip_boy_update.cmd")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write("@echo off\n")
        f.write("title pip-boy updater\n")
        f.write(f"echo [pip-boy] Waiting for current process (PID {pid}) to exit...\n")
        f.write(":wait\n")
        f.write(f'tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL\n')
        f.write("if not errorlevel 1 (\n")
        f.write("    timeout /t 1 /nobreak >NUL\n")
        f.write("    goto wait\n")
        f.write(")\n")
        f.write("echo [pip-boy] Upgrading pip-boy...\n")
        f.write(f'"{python}" -m pip install --upgrade pip-boy\n')
        f.write("if errorlevel 1 (\n")
        f.write("    echo [pip-boy] Update failed. Press any key to close.\n")
        f.write("    pause >NUL\n")
        f.write("    exit /b 1\n")
        f.write(")\n")
        f.write("echo [pip-boy] Restarting pip-boy...\n")
        f.write(f'cd /d "{cwd}"\n')
        f.write(f'"{python}" -m pip_agent\n')

    subprocess.Popen(
        ["cmd", "/c", script_path],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )

    print("  [system] Windows file lock detected.")
    print("  [system] Launched updater in a new window.")
    print("  [system] pip-boy will restart there after the upgrade completes.")
    return CommandResult(
        handled=True,
        exit_requested=True,
        response=f"Upgrading from v{current_ver}... pip-boy will restart in the new window.",
    )


# ---------------------------------------------------------------------------
# /exit
# ---------------------------------------------------------------------------

def _cmd_exit(ctx: CommandContext, args: str) -> CommandResult:
    if ctx.inbound.channel != "cli":
        return CommandResult(
            handled=True,
            response="/exit is only available in the CLI channel.",
        )
    return CommandResult(handled=True, exit_requested=True)
