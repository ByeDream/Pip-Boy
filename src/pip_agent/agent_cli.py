"""CLI REPL for Pip-Boy.

Uses :mod:`pip_agent.agent_runner` to drive Claude through the Agent SDK,
with all unique capabilities exposed as an in-process MCP server.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from pip_agent.agent_runner import QueryResult, run_query
from pip_agent.config import settings
from pip_agent.mcp_tools import McpContext
from pip_agent.routing import (
    AgentRegistry,
    BindingTable,
    build_session_key,
    resolve_effective_config,
)
from pip_agent.tools import WORKDIR

log = logging.getLogger(__name__)


try:
    import readline  # noqa: F401

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
    readline.parse_and_bind("set enable-meta-keybindings on")
except ImportError:
    pass


AGENTS_DIR = WORKDIR / ".pip" / "agents"
BINDINGS_PATH = AGENTS_DIR / "bindings.json"


def run_sdk_cli() -> None:
    """Blocking CLI entry point.  Sets up host services then enters the REPL."""
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    from pip_agent.memory import MemoryStore
    from pip_agent.profiler import Profiler
    from pip_agent.scaffold import ensure_workspace
    from pip_agent.worktree import WorktreeManager

    ensure_workspace(WORKDIR)
    settings.check_required()

    registry = AgentRegistry(AGENTS_DIR)
    binding_table = BindingTable()
    binding_table.load(BINDINGS_PATH)
    default_agent = registry.default_agent()

    profiler = Profiler()

    memory_store = MemoryStore(base_dir=AGENTS_DIR, agent_id=default_agent.id)
    worktree_manager = WorktreeManager(WORKDIR, agent_id=default_agent.id)

    transcripts_dir = AGENTS_DIR / default_agent.id / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    mcp_ctx = McpContext(
        memory_store=memory_store,
        worktree_manager=worktree_manager,
        profiler=profiler,
        workdir=WORKDIR,
        model=default_agent.effective_model,
        transcripts_dir=transcripts_dir,
        sender_id="cli-user",
    )

    # SDK session ID per (agent, cli, user) tuple
    sessions: dict[str, str | None] = {}

    from pip_agent import __version__
    print(
        "============================================\n"
        "  ROBCO INDUSTRIES (TM) TERMLINK PROTOCOL\n"
        "  PIP-BOY 3000 MARK IV  [SDK MODE]\n"
        f"  Personal Assistant Module v{__version__}\n"
        "============================================\n"
        "  Welcome, Vault Dweller. Type '/exit' to\n"
        "  power down.\n"
        f"  Agent: {default_agent.id}\n"
        f"  Model: {default_agent.effective_model}\n"
        "============================================"
    )

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        if user_input.lower() in ("/exit", "exit"):
            break

        # Resolve effective agent config
        aid, bnd = binding_table.resolve(channel="cli", peer_id="cli-user")
        if not aid:
            aid = default_agent.id
        cfg = registry.get_agent(aid) or default_agent
        eff = resolve_effective_config(cfg, bnd)

        # Update MCP context for this agent
        if eff.id != mcp_ctx.memory_store.agent_id:  # type: ignore[union-attr]
            mcp_ctx.memory_store = MemoryStore(AGENTS_DIR, eff.id)
            mcp_ctx.transcripts_dir = AGENTS_DIR / eff.id / "transcripts"
            mcp_ctx.transcripts_dir.mkdir(parents=True, exist_ok=True)
        mcp_ctx.model = eff.effective_model

        # Build system prompt with memory enrichment
        base_prompt = eff.system_prompt(workdir=str(WORKDIR))
        system_prompt = mcp_ctx.memory_store.enrich_prompt(  # type: ignore[union-attr]
            base_prompt, user_input,
            channel="cli", agent_id=eff.id,
            workdir=str(WORKDIR), sender_id="cli-user",
        )

        sk = build_session_key(
            agent_id=eff.id, channel="cli", peer_id="cli-user",
            dm_scope=eff.effective_dm_scope,
        )
        current_session = sessions.get(sk)

        try:
            result: QueryResult = asyncio.run(run_query(
                prompt=user_input,
                mcp_ctx=mcp_ctx,
                model=eff.effective_model,
                session_id=current_session,
                system_prompt_append=system_prompt,
                cwd=WORKDIR,
                verbose=settings.verbose,
            ))
        except KeyboardInterrupt:
            print("\n  [interrupted]")
            continue

        if result.session_id:
            sessions[sk] = result.session_id

        if result.error:
            print(f"\n  [error] {result.error}")
        elif result.text and not settings.verbose:
            print()
            print(result.text)
        elif result.text:
            # In verbose mode text was already streamed
            print()

        profiler.flush()
