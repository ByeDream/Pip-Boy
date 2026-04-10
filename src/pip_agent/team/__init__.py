from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from pip_agent.config import settings
from pip_agent.profiler import Profiler
from pip_agent.tool_dispatch import TeammateToolSurface, ToolContext, dispatch_tool
from pip_agent.tools import VALID_MSG_TYPES, WORKDIR, tools_for_role

if TYPE_CHECKING:
    import anthropic
    from pip_agent.skills import SkillRegistry
    from pip_agent.task_graph import PlanManager
    from pip_agent.worktree import WorktreeManager

MAX_TOOL_OUTPUT = 50_000

TASK_BOARD_HINT = (
    "<task-board-hint>Review the task board.</task-board-hint>"
)

IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, match.group(2).strip()


# ---------------------------------------------------------------------------
# TeammateSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeammateSpec:
    name: str
    description: str
    system_body: str

    @classmethod
    def from_file(cls, path: Path) -> TeammateSpec:
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        return cls(
            name=meta.get("name", path.stem),
            description=meta.get("description", ""),
            system_body=body,
        )

    def to_frontmatter(self) -> str:
        lines = [
            "---",
            f"name: {self.name}",
            f'description: "{self.description}"',
            "---",
        ]
        if self.system_body:
            lines.append("")
            lines.append(self.system_body)
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------


class Bus:
    """JSONL file-based message bus. Append-only send, drain-on-read."""

    def __init__(self, inbox_dir: Path) -> None:
        self._dir = inbox_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def send(
        self,
        from_name: str,
        to_name: str,
        content: str,
        msg_type: str = "message",
        **extra,
    ) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return (
                f"[error] Invalid msg_type '{msg_type}'. "
                f"Valid: {sorted(VALID_MSG_TYPES)}"
            )
        msg = {
            "type": msg_type,
            "from": from_name,
            "content": content,
            "ts": time.time(),
        }
        msg.update(extra)
        line = json.dumps(msg)
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(
                self._dir / f"{to_name}.jsonl", "a", encoding="utf-8",
            ) as f:
                f.write(line + "\n")
        return f"Sent {msg_type} to {to_name}"

    def _parse_inbox(self, path: Path) -> list[dict]:
        messages: list[dict] = []
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if line.strip():
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return messages

    def peek_inbox(self, name: str) -> list[dict]:
        path = self._dir / f"{name}.jsonl"
        with self._lock:
            if not path.is_file() or path.stat().st_size == 0:
                return []
            return self._parse_inbox(path)

    def read_inbox(self, name: str) -> list[dict]:
        path = self._dir / f"{name}.jsonl"
        with self._lock:
            if not path.is_file() or path.stat().st_size == 0:
                return []
            messages = self._parse_inbox(path)
            path.write_text("", encoding="utf-8")
        return messages


# ---------------------------------------------------------------------------
# ProtocolTracker
# ---------------------------------------------------------------------------


class ProtocolTracker:
    """Track request-response protocol state (shutdown, plan approval).

    Shared FSM: [pending] --approve--> [approved]
                [pending] --reject---> [rejected]
    """

    def __init__(self) -> None:
        self._shutdown: dict[str, dict] = {}
        self._plans: dict[str, dict] = {}
        self._lock = threading.Lock()

    def open_shutdown(self, target: str) -> str:
        req_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._shutdown[req_id] = {"target": target, "status": "pending"}
        return req_id

    def open_plan(self, from_name: str, plan: str) -> str:
        req_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._plans[req_id] = {
                "from": from_name, "plan": plan, "status": "pending",
            }
        return req_id

    def resolve(self, req_id: str, approve: bool) -> str:
        new_status = "approved" if approve else "rejected"
        with self._lock:
            for store in (self._shutdown, self._plans):
                if req_id in store:
                    if store[req_id]["status"] != "pending":
                        return (
                            f"[error] Request {req_id} already "
                            f"{store[req_id]['status']}"
                        )
                    store[req_id]["status"] = new_status
                    return new_status
        return f"[error] Unknown request_id '{req_id}'"

    def get(self, req_id: str) -> dict | None:
        with self._lock:
            for store in (self._shutdown, self._plans):
                if req_id in store:
                    return dict(store[req_id])
        return None


# ---------------------------------------------------------------------------
# Teammate
# ---------------------------------------------------------------------------


def _format_team_message(msg: dict) -> str:
    from_name = msg.get("from", "unknown")
    msg_type = msg.get("type", "message")
    content = msg.get("content", "")
    attrs = f'from="{from_name}" msg_type="{msg_type}"'
    if "req_id" in msg:
        attrs += f' req_id="{msg["req_id"]}"'
    if "approve" in msg:
        attrs += f' approve="{msg["approve"]}"'
    return f"<team-message {attrs}>\n{content}\n</team-message>"


def _dump_messages(agent_name: str, messages: list[dict]) -> None:
    """Print messages structure with tool_use/tool_result pairing for diagnostics."""
    print(f"  [{agent_name}] === MESSAGE DUMP ({len(messages)} msgs) ===")
    pending_tool_ids: set[str] = set()
    for i, msg in enumerate(messages):
        role = msg["role"]
        content = msg.get("content")
        if isinstance(content, str):
            print(f"  [{agent_name}]   [{i}] {role}: text({len(content)} chars)")
            continue
        if not isinstance(content, list):
            print(f"  [{agent_name}]   [{i}] {role}: {type(content).__name__}")
            continue
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "?")
                if btype == "tool_use":
                    bid = block.get("id", "?")
                    parts.append(f"tool_use:{block.get('name','?')}({bid[-8:]})")
                    pending_tool_ids.add(bid)
                elif btype == "tool_result":
                    bid = block.get("tool_use_id", "?")
                    parts.append(f"tool_result({bid[-8:]})")
                    pending_tool_ids.discard(bid)
                elif btype == "text":
                    parts.append(f"text({len(block.get('text',''))}ch)")
                else:
                    parts.append(btype)
            elif hasattr(block, "type"):
                if block.type == "tool_use":
                    parts.append(f"tool_use:{block.name}({block.id[-8:]})")
                    pending_tool_ids.add(block.id)
                elif hasattr(block, "text"):
                    parts.append(f"text({len(block.text)}ch)")
                else:
                    parts.append(block.type)
        print(f"  [{agent_name}]   [{i}] {role}: [{', '.join(parts)}]")
        if pending_tool_ids and role == "user":
            print(f"  [{agent_name}]   ^^^ ORPHAN tool_use IDs still pending: "
                  f"{[tid[-8:] for tid in pending_tool_ids]}")
    if pending_tool_ids:
        print(f"  [{agent_name}]   FINAL ORPHANS: {[tid[-8:] for tid in pending_tool_ids]}")
    print(f"  [{agent_name}] === END DUMP ===")


class Teammate:
    """Autonomous agent on a daemon thread.

    Lifecycle: WAIT → WORK ⇄ IDLE → OFFLINE.
    Idle polls the inbox; when the task board has claimable work and the
    board revision is new since the last overview/detail/claim, a short
    hint message is injected (no automatic claim).
    """

    LEAD = "lead"

    def __init__(
        self,
        spec: TeammateSpec,
        client: anthropic.Anthropic,
        bus: Bus,
        profiler: Profiler,
        *,
        model: str,
        max_turns: int,
        protocol: ProtocolTracker | None = None,
        skill_registry: SkillRegistry | None = None,
        active_names_fn: callable = lambda: [],
        done_fn: callable | None = None,
        plan_manager: PlanManager | None = None,
        worktree_manager: WorktreeManager | None = None,
    ) -> None:
        self.spec = spec
        self._model = model
        self._client = client
        self._bus = bus
        self._profiler = profiler
        self._protocol = protocol
        self._skill_registry = skill_registry
        self._active_names_fn = active_names_fn
        self._done_fn = done_fn
        self._plan_manager = plan_manager
        self._worktree_manager = worktree_manager
        self._max_turns = max_turns
        self._status = "working"
        self._shutdown = threading.Event()
        self._approved_shutdown = False
        self._idle_requested = False
        self._offline_reason = "unknown"
        self._board_revision_seen: int | None = None

    @property
    def status(self) -> str:
        return self._status

    def start(self) -> None:
        self._shutdown.clear()
        self._status = "working"
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def stop(self) -> None:
        self._shutdown.set()

    # -- Work-idle lifecycle ------------------------------------------------

    def _run(self) -> None:
        """Wait for inbox, then loop: WORK -> IDLE -> WORK -> ... -> OFFLINE."""
        try:
            self._run_inner()
        except Exception as exc:
            self._offline_reason = f"crashed: {exc}"
            if settings.verbose:
                import traceback
                print(f"  [{self.spec.name}] CRASHED: {exc}")
                traceback.print_exc()
            self._finish()

    def _run_inner(self) -> None:
        while not self._shutdown.is_set():
            inbox = self._bus.read_inbox(self.spec.name)
            if inbox:
                break
            self._shutdown.wait(timeout=2)
        else:
            self._offline_reason = "shutdown before start"
            self._finish()
            return

        messages: list[dict] = []
        self._work(messages, inbox)

        while not self._shutdown.is_set() and not self._approved_shutdown:
            self._status = "idle"
            work = self._idle_cycle()
            if work is None:
                break
            self._reinject_identity(messages)
            self._status = "working"
            self._work(messages, work)

        if self._approved_shutdown:
            self._offline_reason = "shutdown approved"
        self._finish()

    def _finish(self) -> None:
        self._status = "offline"
        if settings.verbose:
            print(f"  [{self.spec.name}] going offline ({self._offline_reason})")
        self._bus.send(
            self.spec.name, self.LEAD,
            f"Going offline. Reason: {self._offline_reason}",
            "status",
        )
        if self._done_fn:
            self._done_fn(self.spec.name)

    # -- WORK phase ---------------------------------------------------------

    def _work(
        self, messages: list[dict], inbox_messages: list[dict],
    ) -> None:
        """Run one LLM work cycle for a batch of inbox messages."""
        text = "\n".join(_format_team_message(m) for m in inbox_messages)
        new_content = [{"type": "text", "text": text}]

        if messages and messages[-1]["role"] == "user":
            last = messages[-1]
            if isinstance(last["content"], list):
                last["content"].extend(new_content)
            else:
                messages.append({"role": "user", "content": new_content})
        else:
            messages.append({"role": "user", "content": new_content})

        tools = self._build_tools()
        system = self._system_prompt()
        self._idle_requested = False

        for _ in range(self._max_turns):
            self._inject_inbox(messages)
            if self._approved_shutdown or self._idle_requested:
                break

            self._profiler.start(f"api:teammate:{self.spec.name}")
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=settings.max_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
            except Exception as exc:
                if settings.verbose:
                    print(f"  [{self.spec.name}] API error: {exc}")
                    _dump_messages(self.spec.name, messages)
                break
            usage = response.usage
            self._profiler.stop(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                stop=response.stop_reason,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                if settings.verbose:
                    for block in response.content:
                        if hasattr(block, "text"):
                            print(f"  [{self.spec.name}] {block.text}")
                break

            tool_results: list[dict] = []
            for block in response.content:
                if block.type != "tool_use":
                    if settings.verbose and hasattr(block, "text"):
                        print(f"  [{self.spec.name}] {block.text}")
                    continue
                if settings.verbose:
                    print(f"  [{self.spec.name}] > {block.name}")
                try:
                    result = self._exec_tool(block.name, block.input)
                except Exception as exc:
                    result = f"[tool error] {exc}"
                    if settings.verbose:
                        print(f"  [{self.spec.name}] tool {block.name} crashed: {exc}")
                if len(result) > MAX_TOOL_OUTPUT:
                    result = result[:MAX_TOOL_OUTPUT] + "\n\n[truncated]"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})

            if self._idle_requested:
                break
        else:
            self._offline_reason = "max_turns exhausted"
            if settings.verbose:
                print(f"  [{self.spec.name}] max_turns ({self._max_turns}) exhausted")
            self._bus.send(
                self.spec.name, self.LEAD,
                f"I've used all {self._max_turns} turns and need more. "
                f"Re-spawn me to continue.",
                "status",
            )

    # -- IDLE phase ---------------------------------------------------------

    def _idle_cycle(self) -> list[dict] | None:
        """Poll inbox; optional task-board hint if claimable work and dirty revision."""
        if settings.verbose:
            print(f"  [{self.spec.name}] entering idle (timeout={IDLE_TIMEOUT}s)")
        deadline = time.time() + IDLE_TIMEOUT
        while time.time() < deadline and not self._shutdown.is_set():
            inbox = self._bus.read_inbox(self.spec.name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        return None
                if settings.verbose:
                    print(f"  [{self.spec.name}] idle: inbox received, resuming work")
                return inbox

            hint = self._maybe_task_board_hint()
            if hint is not None:
                if settings.verbose:
                    print(
                        f"  [{self.spec.name}] idle: task board hint "
                        f"(claimable + dirty revision)"
                    )
                return hint

            self._shutdown.wait(timeout=IDLE_POLL_INTERVAL)
        self._offline_reason = "idle timeout"
        if settings.verbose:
            print(f"  [{self.spec.name}] idle timeout, going offline")
        return None

    def _task_board_dirty(self) -> bool:
        if self._plan_manager is None:
            return False
        rev = self._plan_manager.board_revision
        seen = self._board_revision_seen
        return seen is None or seen != rev

    def _maybe_task_board_hint(self) -> list[dict] | None:
        """If claimable work exists and board is dirty, inject a hint (no claim)."""
        if self._plan_manager is None:
            return None
        if not self._task_board_dirty():
            return None
        if not self._plan_manager.has_claimable_work():
            return None
        self._board_revision_seen = self._plan_manager.board_revision
        return [{
            "type": "message",
            "from": "task_board",
            "content": TASK_BOARD_HINT,
        }]

    def _reinject_identity(self, messages: list[dict]) -> None:
        """Insert identity message-pair when context is thin."""
        if len(messages) > 3:
            return
        identity = (
            f"<identity>You are '{self.spec.name}'. "
            f"{self.spec.system_body}</identity>"
        )
        messages.insert(
            0, {"role": "user", "content": identity},
        )
        messages.insert(
            1, {"role": "assistant", "content": f"I am {self.spec.name}. Continuing."},
        )

    def _inject_inbox(self, messages: list[dict]) -> None:
        """Drain inbox and inject new messages into the last user turn."""
        new_inbox = self._bus.read_inbox(self.spec.name)
        if not new_inbox:
            return
        parts = [_format_team_message(msg) for msg in new_inbox]
        last = messages[-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            for text in parts:
                last["content"].append({"type": "text", "text": text})
        elif settings.verbose:
            print(
                f"  [{self.spec.name}] inbox DROPPED {len(parts)} msg(s), "
                f"last role={last['role']}, "
                f"content type={type(last.get('content')).__name__}"
            )

    # -- Tool dispatch ------------------------------------------------------

    def _teammate_tool_surface(self) -> TeammateToolSurface:
        return TeammateToolSurface(
            send=self._handle_send,
            read_inbox=self._surface_read_inbox,
            request_idle=self._surface_request_idle,
        )

    def _surface_read_inbox(self) -> str:
        msgs = self._bus.read_inbox(self.spec.name)
        if not msgs:
            return "(no messages)"
        return json.dumps(msgs, indent=2)

    def _surface_request_idle(self) -> None:
        self._idle_requested = True

    def _exec_tool(self, name: str, tool_input: dict) -> str:
        ctx = ToolContext(
            profiler=self._profiler,
            plan_manager=self._plan_manager,
            skill_registry=self._skill_registry,
            worktree_manager=self._worktree_manager,
            teammate=self._teammate_tool_surface(),
            caller=self.spec.name,
        )
        result = dispatch_tool(ctx, name, tool_input).content
        self._maybe_mark_board_seen(name, result)
        return result

    def _maybe_mark_board_seen(self, tool_name: str, result: str) -> None:
        if self._plan_manager is None:
            return
        if tool_name not in (
            "task_board_overview",
            "task_board_detail",
            "claim_task",
        ):
            return
        if "Unknown tool:" in result:
            return
        if result.startswith("[error]") or result.startswith("[blocked]"):
            return
        self._board_revision_seen = self._plan_manager.board_revision

    def _handle_send(self, tool_input: dict) -> str:
        to = tool_input["to"]
        content = tool_input["content"]
        msg_type = tool_input.get("msg_type", "message")
        extra: dict = {}
        for key in ("req_id", "approve"):
            if key in tool_input:
                extra[key] = tool_input[key]
        if msg_type == "broadcast":
            count = 0
            for name in self._active_names_fn():
                if name != self.spec.name:
                    self._bus.send(self.spec.name, name, content, "broadcast")
                    count += 1
            self._bus.send(self.spec.name, self.LEAD, content, "broadcast")
            return f"Broadcast to {count} teammates + lead"
        if self._protocol is not None:
            if msg_type == "plan_request":
                extra["req_id"] = self._protocol.open_plan(
                    self.spec.name, content,
                )
            elif msg_type == "shutdown_response":
                req_id = extra.get("req_id")
                approve = extra.get("approve", False)
                if req_id:
                    self._protocol.resolve(req_id, approve)
                if approve:
                    self._approved_shutdown = True
        return self._bus.send(self.spec.name, to, content, msg_type, **extra)

    # -- Tool & prompt construction -----------------------------------------

    def _build_tools(self) -> list[dict]:
        tools = list(tools_for_role("teammate"))
        if self._plan_manager is None:
            _plan_tools = {
                "claim_task", "task_board_overview",
                "task_board_detail", "task_submit",
            }
            tools = [t for t in tools if t["name"] not in _plan_tools]
        if self._skill_registry is not None and self._skill_registry.available:
            tools.append(self._skill_registry.tool_schema())
        return tools

    def _system_prompt(self) -> str:
        wt = self._worktree_manager
        if wt is not None and wt.exists(self.spec.name):
            workdir = str(wt.worktree_path(self.spec.name))
        else:
            workdir = str(WORKDIR)

        base = (
            f"You are '{self.spec.name}', a subagent in an agent team.\n"
            f"Working directory: {workdir}\n"
            f"Use the 'send' tool to communicate with teammates or 'lead'.\n"
        )
        if self._plan_manager is not None:
            base += (
                "Task board: task_board_overview, task_board_detail, claim_task.\n"
                "When done: use task_submit(story, task_id) to submit for Lead review.\n"
                "If task_submit reports conflicts (status=failed), resolve the "
                "conflict files, commit, then call task_submit again.\n"
            )
        if self.spec.system_body:
            return base + "\n" + self.spec.system_body
        return base


# ---------------------------------------------------------------------------
# TeamManager
# ---------------------------------------------------------------------------


class TeamManager:
    """Orchestrate teammate discovery, spawning, and communication."""

    LEAD = "lead"

    def __init__(
        self,
        builtin_dir: Path,
        user_dir: Path,
        client: anthropic.Anthropic,
        profiler: Profiler,
        *,
        skill_registry: SkillRegistry | None = None,
        plan_manager: PlanManager | None = None,
        worktree_manager: WorktreeManager | None = None,
    ) -> None:
        self._client = client
        self._profiler = profiler
        self._skill_registry = skill_registry
        self._plan_manager = plan_manager
        self._worktree_manager = worktree_manager
        self._roster: dict[str, TeammateSpec] = {}
        self._active: dict[str, Teammate] = {}
        self._bus = Bus(user_dir / "inbox")
        self._protocol = ProtocolTracker()
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir

        self._scan_dir(builtin_dir)
        self._scan_dir(user_dir)

    def _scan_dir(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for md_path in sorted(directory.glob("*.md")):
            try:
                spec = TeammateSpec.from_file(md_path)
                self._roster[spec.name] = spec
            except Exception:
                continue

    def _active_names(self) -> list[str]:
        return list(self._active.keys())

    def _on_done(self, name: str) -> None:
        self._active.pop(name, None)

    def _make_teammate(
        self, spec: TeammateSpec, *, model: str, max_turns: int,
    ) -> Teammate:
        return Teammate(
            spec,
            self._client,
            self._bus,
            self._profiler,
            model=model,
            max_turns=max_turns,
            protocol=self._protocol,
            skill_registry=self._skill_registry,
            active_names_fn=self._active_names,
            done_fn=self._on_done,
            plan_manager=self._plan_manager,
            worktree_manager=self._worktree_manager,
        )

    # -- Public API (called from agent_loop) --------------------------------

    def _rescan(self) -> None:
        self._scan_dir(self._builtin_dir)
        self._scan_dir(self._user_dir)

    def patch_model_enum(self, tools: list[dict]) -> None:
        """Inject valid model IDs as enum into team_spawn schema."""
        valid = sorted(self._valid_models())
        if not valid:
            return
        for tool in tools:
            if tool.get("name") == "team_spawn":
                tool["input_schema"]["properties"]["model"]["enum"] = valid
                break

    def _valid_models(self) -> set[str]:
        path = self._user_dir.parent / "models.json"
        if not path.is_file():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {m["id"] for m in data if "id" in m}
        except (json.JSONDecodeError, OSError, TypeError):
            return set()

    def spawn(
        self, name: str, prompt: str, *, model: str, max_turns: int,
    ) -> str:
        if name in self._active:
            state = self._active[name].status
            return f"[error] '{name}' is currently {state}."
        valid = self._valid_models()
        if valid and model not in valid:
            return (
                f"[error] Unknown model '{model}'. "
                f"Available: {', '.join(sorted(valid))}"
            )
        spec = self._roster.get(name)
        if spec is None:
            self._rescan()
            spec = self._roster.get(name)
        if spec is None:
            available = ", ".join(sorted(self._roster.keys())) or "(none)"
            return f"[error] Unknown teammate '{name}'. Available: {available}"
        teammate = self._make_teammate(spec, model=model, max_turns=max_turns)
        teammate.start()
        self._active[name] = teammate
        self._bus.send(self.LEAD, name, prompt, "message")
        return f"Spawned '{name}' ({model}, max {max_turns} turns)."

    def send(
        self, to: str, content: str, msg_type: str = "message", **extra,
    ) -> str:
        if msg_type == "broadcast":
            count = 0
            for name in list(self._active):
                self._bus.send(self.LEAD, name, content, "broadcast")
                count += 1
            return f"Broadcast to {count} teammates."
        if msg_type == "shutdown_request":
            extra["req_id"] = self._protocol.open_shutdown(to)
        elif msg_type == "plan_response":
            req_id = extra.get("req_id")
            approve = extra.get("approve", False)
            if req_id:
                self._protocol.resolve(req_id, approve)
        result = self._bus.send(self.LEAD, to, content, msg_type, **extra)
        if to not in self._active:
            return f"{result} (offline — will be read on next activation)"
        return result

    def peek_inbox(self) -> list[dict]:
        return self._bus.peek_inbox(self.LEAD)

    def read_inbox(self) -> list[dict]:
        return self._bus.read_inbox(self.LEAD)

    def status(self) -> str:
        self._rescan()
        if not self._roster:
            return "No teammates defined."
        lines: list[str] = []
        for name in sorted(self._roster):
            spec = self._roster[name]
            if name in self._active:
                tm = self._active[name]
                lines.append(
                    f"  {name} [{tm.status}] {spec.description}"
                    f" ({tm._model}, {tm._max_turns} turns)"
                )
            else:
                lines.append(f"  {name} [offline] {spec.description}")
        return "\n".join(lines)

    def create_teammate(self, name: str, description: str, system_prompt: str) -> str:
        if name in self._roster:
            return f"[error] Teammate '{name}' already exists."
        spec = TeammateSpec(name=name, description=description, system_body=system_prompt)
        path = self._user_dir / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec.to_frontmatter(), encoding="utf-8")
        self._roster[name] = spec
        return f"Created teammate '{name}' at {path.relative_to(WORKDIR)}"

    def edit_teammate(self, name: str, **updates: str) -> str:
        spec = self._roster.get(name)
        if spec is None:
            self._rescan()
            spec = self._roster.get(name)
        if spec is None:
            return f"[error] Unknown teammate '{name}'."
        new_desc = updates.get("description", spec.description)
        new_body = updates.get("system_prompt", spec.system_body)
        new_spec = TeammateSpec(name=name, description=new_desc, system_body=new_body)
        path = self._user_dir / f"{name}.md"
        if not path.is_file():
            path = self._builtin_dir / f"{name}.md"
        path.write_text(new_spec.to_frontmatter(), encoding="utf-8")
        self._roster[name] = new_spec
        return f"Updated teammate '{name}'."

    def delete_teammate(self, name: str) -> str:
        if name in self._active:
            return f"[error] '{name}' is currently active. Stop it first."
        path = self._user_dir / f"{name}.md"
        if not path.is_file():
            path = self._builtin_dir / f"{name}.md"
        if not path.is_file():
            return f"[error] Teammate '{name}' not found."
        path.unlink()
        self._roster.pop(name, None)
        return f"Deleted teammate '{name}'."

    def list_models(self) -> str:
        path = self._user_dir.parent / "models.json"
        if not path.is_file():
            return "[error] No models.json found in .pip/"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return f"[error] Failed to read models.json: {exc}"
        return json.dumps(data, indent=2)

    def deactivate_all(self) -> None:
        for t in self._active.values():
            t.stop()
        self._active.clear()
