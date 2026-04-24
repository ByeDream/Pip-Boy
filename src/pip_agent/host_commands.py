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

  /bind <id>                    Route this chat to sub-agent <id>
                                (bind pip-boy is a redirect to /unbind)
  /unbind                       Clear this chat's binding so routing
                                falls back to pip-boy (no-op when
                                already on pip-boy)

  /subagent                     pip-boy only: list known sub-agents
  /subagent create <label>      pip-boy only, owner: create a new sub-agent.
      [--id ID]                 Default: normalize(<label>) — lowercased,
                                safe for a directory name.
      [--name NAME]             Default: same as id. Human-facing display
                                name; can be mixed-case, spaces, CJK.
      [--model MODEL]           Default: pip-boy's model.
      [--dm_scope SCOPE]        Default: per-guild.
                                Valid: main | per-guild | per-guild-peer.
  /subagent archive <id>        pip-boy only, owner: move <id>/.pip/ to
                                .pip/archived/ (project files untouched)
  /subagent delete <id> --yes   pip-boy only, owner: wipe <id>/.pip/
                                (project files untouched)
  /subagent reset <id>          pip-boy only, owner: rebuild sub-agent
                                <id>'s .pip/ from a minimal backup —
                                persona.md and HEARTBEAT.md are
                                preserved, everything else is wiped
                                and re-created. Not allowed on pip-boy
                                itself (the running host is the one
                                thing we can't safely self-surgery);
                                stop the host and rebuild out-of-band
                                if you really need to.

Per-agent settings after creation (model, dm_scope, description) are
edited directly on disk:
  <workspace>/<id>/.pip/persona.md       (name, model, dm_scope)
  <workspace>/.pip/agents_registry.json  (description)
  <workspace>/.pip/bindings.json         (routing bindings)

Permissions:
  Owner (CLI or an identity listed in owner.md) can use all commands.
  Admin users can read everything and /bind/unbind, but not
  /subagent create|archive|delete|reset or /admin.
  Others are locked out.

Bindings:
  /bind in a group chat creates a guild-level binding; in a private
  chat, a peer-level binding. Bindings persist across restarts in
  <workspace>/.pip/bindings.json. /unbind removes the binding for
  the current chat so routing falls back to pip-boy."""


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


_SUBAGENT_OWNER_ONLY_SUBCOMMANDS = {"create", "archive", "delete", "reset"}


def _is_owner(ctx: CommandContext) -> bool:
    """Match the ACL rules in :func:`dispatch_command`."""
    ch, sid = ctx.inbound.channel, ctx.inbound.sender_id
    if ch == "cli":
        return True
    ms = ctx.memory_store
    return bool(ms and ms.is_owner(ch, sid))


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

    Owner gating
    ------------
    Read-only ops (``list``, bare ``/subagent``) ride the top-level
    "owner-or-admin" gate from :func:`dispatch_command`. Destructive
    ops (``create``, ``archive``, ``delete``, ``reset``) re-check
    owner here.
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

    if sub in _SUBAGENT_OWNER_ONLY_SUBCOMMANDS and not _is_owner(ctx):
        return CommandResult(
            handled=True,
            response=f"Permission denied: `/subagent {sub}` is owner only.",
        )
    return handler(ctx, tail)


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
    lines.append(
        "\n* = currently routed for this chat. "
        "Use `/bind <id>` to change, `/unbind` to return to pip-boy.",
    )
    return CommandResult(handled=True, response="\n".join(lines))


_VALID_DM_SCOPES = {"main", "per-guild", "per-guild-peer"}

_CREATE_USAGE = (
    "Usage: /subagent create <label> [--id ID] [--name NAME] "
    "[--model MODEL] [--dm_scope SCOPE]\n"
    "The positional <label> is the directory name under the workspace "
    "root. --id is the agent's identity key (registry + session + bind "
    "target); it defaults to <label> when omitted, so the two stay in "
    "sync for the simple case. Provide --id to decouple them.\n"
    "Defaults: --name <id>, --model <root agent's model>, "
    "--dm_scope per-guild.\n"
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

    root_cfg = ctx.registry.default_agent()
    model = opts.get("--model") or (root_cfg.model or "")

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

    The new agent inherits the default (``pip-boy``) agent's **full**
    persona body — Core Philosophy, System Communication, Tone,
    Identity Recognition, Tool Calling, Memory guidance, etc. — so
    it actually knows how to interpret the ``# User`` block that
    :meth:`MemoryStore.enrich_prompt` injects at prompt time.

    Only the ``# Identity`` section is rewritten, to flag the
    shared-owner relationship with Pip-Boy. The identity body still
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
    state.json, users/, incoming/, cron.json, sdk_sessions entries
    for this agent, .scaffold_manifest.json, ...) wiped and left to
    be lazily re-created by the running host.

    Root (pip-boy) refusal
    ----------------------
    ``/subagent reset pip-boy`` is rejected outright. The root agent's
    ``.pip/`` carries workspace-shared state (``owner.md``,
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
                "workspace-shared state (owner.md, bindings.json, "
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
        # with ENOENT on ``observations/<date>.jsonl``.
        (pip_dir / "observations").mkdir(exist_ok=True)
        (pip_dir / "users").mkdir(exist_ok=True)
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
# /switch, /reset removed — functionality consolidated into
# `/subagent <subcommand>`, `/bind`, `/unbind` above. The earlier
# ``/agent`` umbrella was renamed to ``/subagent`` so the verb matches
# what it actually does (manage siblings), and the asymmetric
# ``/agent switch`` + ``/home`` pair was replaced by the symmetric
# ``/bind`` / ``/unbind`` pair that works from any agent.)
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
    "/subagent": _cmd_subagent,
    "/bind": _cmd_bind,
    "/unbind": _cmd_unbind,
    "/exit": _cmd_exit,
}

_OPEN_COMMANDS = {"/help", "/status"}
# ``/subagent create`` / ``archive`` / ``delete`` / ``reset`` are
# owner-only, but since they're all subcommands of a single top-level
# ``/subagent``, we gate per-subcommand inside the dispatcher rather
# than at this top-level ACL table. Pure-read ``/subagent [list]``
# and ``/bind`` / ``/unbind`` remain owner-or-admin (the default).
_OWNER_ONLY_COMMANDS = {"/admin"}
