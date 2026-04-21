"""Host-level scheduler: cron jobs + heartbeat pings.

The scheduler is intentionally lean. It does not run tool calls, touch memory,
or talk to the SDK directly. Its only job is to push ``InboundMessage``
instances into the host's inbound queue at the right time. The agent then
processes them through the same code path as any user-sent message.

Design:

* **One background thread per ``AgentHost``.** Ticks every ``_TICK_SECONDS``.
* **Per-agent cron store** at ``.pip/agents/<agent_id>/cron.json`` — a list of
  job dicts. Stored on disk so jobs survive restarts.
* **Per-agent heartbeat source** at ``.pip/agents/<agent_id>/HEARTBEAT.md``. If
  the file exists, the agent receives a ``<heartbeat>`` inbound every
  ``settings.heartbeat_interval`` seconds during the active window.
* **Sentinel sender ids** (see :class:`_Sender`) mark host-injected messages.
  Phase 4.6 uses these to wrap the prompt with ``<cron_task>`` / ``<heartbeat>``
  tags instead of ``<user_query>``.

Schedule kinds supported:

* ``at``       — one-shot, fires at an absolute epoch timestamp, then disables.
* ``every``    — repeating, fires every ``seconds`` interval.
* ``cron``     — minimal cron expression parser supporting ``"M H * * *"``
  (daily) and ``"M * * * *"`` (hourly). Anything more exotic returns an error
  at ``add_job`` time.

See ``docs/sdk-contract-notes.md`` for the full host ⇄ agent contract.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pip_agent.channels import InboundMessage
from pip_agent.config import settings
from pip_agent.fileutil import atomic_write

log = logging.getLogger(__name__)

_TICK_SECONDS = 5.0
_CRON_FILE = "cron.json"
_HEARTBEAT_FILE = "HEARTBEAT.md"

# Cron auto-disable: after this many consecutive failed runs, flip
# ``enabled=False`` on the job so we stop burning an SDK cold start every
# interval on something that is broken. The count resets to 0 on the first
# successful run. ``run_job`` / ``remove_job`` / ``update_job`` can always
# flip it back on manually.
_CRON_AUTO_DISABLE_THRESHOLD = 5

# Dream state keys kept in ``state.json`` per agent.
_DREAM_LAST_AT_KEY = "last_dream_at"
_DREAM_CYCLE_KEY = "dream_cycle_count"
# ``_stop_hook`` stamps this on every completed agent turn. Dream's idle
# gate compares against it. Defined here to keep the contract visible
# from the scheduler side.
_LAST_ACTIVITY_KEY = "last_activity_at"


class _Sender:
    CRON = "__cron__"
    HEARTBEAT = "__heartbeat__"


def _pending_key_cron(job_id: str) -> str:
    """Coalesce key for a cron job.

    Cron jobs are uniquely identified by their 8-char ``id``, so the key is
    per-job. Two different cron jobs can be in-flight simultaneously; the
    same job cannot.
    """
    return f"cron:{job_id}"


def _pending_key_heartbeat(agent_id: str) -> str:
    """Coalesce key for a heartbeat.

    Heartbeats are a per-agent signal (one ``HEARTBEAT.md`` per agent
    directory), so the key is per-agent.
    """
    return f"hb:{agent_id}"


@dataclass
class _TrackedInbound:
    """Handle yielded by :meth:`HostScheduler.track`.

    ``failure(msg)`` marks the current run as a logical failure so the
    scheduler will (a) bump the cron job's ``consecutive_errors`` counter
    and (b) auto-disable at :data:`_CRON_AUTO_DISABLE_THRESHOLD`. Only
    ``AgentHost.process_inbound`` should call it directly; uncaught
    exceptions inside the ``with`` block also count as failure.
    """

    inbound: InboundMessage
    failed: bool = False
    error: str = ""

    def failure(self, message: str = "") -> None:
        self.failed = True
        if message:
            self.error = message


@dataclass
class _HeartbeatState:
    """Per-agent heartbeat bookkeeping.

    ``last_fire_at`` is seeded with the current epoch when the state is first
    created, NOT ``0.0``. Rationale: the very first SDK turn after startup
    is a cold start (typically 30–90 s while Claude Code spins up its
    subprocess and loads the JSONL transcript). If ``last_fire_at`` defaulted
    to ``0.0``, the ``elapsed >= interval`` check would pass on tick #1 and
    the heartbeat would immediately front-run the user — the cold-start
    latency gets burned on a background keepalive while the user's first
    ``你好`` sits in the queue. Seeding with ``now`` delays the first fire
    by ``heartbeat_interval`` seconds, which is what the user expects.
    """

    last_fire_at: float


# ---------------------------------------------------------------------------
# Schedule maths
# ---------------------------------------------------------------------------


def _next_fire_at(kind: str, cfg: dict[str, Any], *, now: float) -> float | None:
    """Return the next fire epoch, or ``None`` if the schedule is invalid/exhausted.

    Callers are expected to validate ``kind`` and ``cfg`` beforehand; this
    function is defensive but doesn't surface diagnostic strings.
    """
    if kind == "at":
        ts = float(cfg.get("timestamp", 0))
        if ts <= 0:
            return None
        return ts if ts > now else None
    if kind == "every":
        secs = int(cfg.get("seconds", 0))
        if secs <= 0:
            return None
        return now + secs
    if kind == "cron":
        return _next_cron_fire(cfg.get("expr", ""), now=now)
    return None


def _next_cron_fire(expr: str, *, now: float) -> float | None:
    """Compute the next fire time for a minimal cron expression.

    Supported forms:

    * ``"M H * * *"`` — fire daily at ``H:M`` local time.
    * ``"M * * * *"`` — fire hourly at minute ``M``.

    Returns ``None`` for anything outside this grammar. Phase 11 (or a later
    revision) can swap this out for ``croniter`` if we need day-of-week etc.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    minute_s, hour_s, dom, mon, dow = parts
    if dom != "*" or mon != "*" or dow != "*":
        return None
    try:
        minute = int(minute_s)
    except ValueError:
        return None
    if not 0 <= minute <= 59:
        return None

    local_now = datetime.fromtimestamp(now)

    if hour_s == "*":
        target = local_now.replace(minute=minute, second=0, microsecond=0)
        ts = target.timestamp()
        while ts <= now:
            ts += 3600
        return ts

    try:
        hour = int(hour_s)
    except ValueError:
        return None
    if not 0 <= hour <= 23:
        return None

    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    ts = target.timestamp()
    while ts <= now:
        ts += 86400
    return ts


def _validate_schedule(kind: str, cfg: dict[str, Any]) -> str | None:
    """Return an error message if ``(kind, cfg)`` is unsupported, else ``None``."""
    if kind == "at":
        ts = cfg.get("timestamp")
        if not isinstance(ts, (int, float)) or float(ts) <= 0:
            return "schedule_kind='at' requires schedule_config.timestamp (epoch)."
        return None
    if kind == "every":
        secs = cfg.get("seconds")
        if not isinstance(secs, int) or secs <= 0:
            return "schedule_kind='every' requires schedule_config.seconds > 0."
        return None
    if kind == "cron":
        expr = cfg.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            return "schedule_kind='cron' requires schedule_config.expr."
        # Probe the parser with a fixed ``now`` so we surface the error here
        # rather than silently dropping jobs during the tick loop.
        if _next_cron_fire(expr, now=time.time()) is None:
            return (
                "Unsupported cron expression. Supported forms: "
                "'M H * * *' (daily) or 'M * * * *' (hourly)."
            )
        return None
    return f"Unknown schedule_kind: {kind!r}."


# ---------------------------------------------------------------------------
# Heartbeat active window
# ---------------------------------------------------------------------------


def _in_active_window(now: float) -> bool:
    """Return True if ``now`` (local epoch) falls inside the heartbeat active window.

    Respects wrap-around (e.g. ``start=22``, ``end=6`` means "late night").
    """
    start = int(settings.heartbeat_active_start) % 24
    end = int(settings.heartbeat_active_end) % 24
    hour = datetime.fromtimestamp(now).hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _in_dream_window(now: float) -> bool:
    """Return True if ``now`` (local epoch) falls inside the Dream idle window.

    Semantics match ``_in_active_window``: same hour → disabled (not "always on"
    like heartbeat — Dream is opt-in; if you want it disabled, set
    ``DREAM_HOUR_START == DREAM_HOUR_END`` and we honour that). Respects
    wrap-around for the late-night-early-morning 22→5 style window.
    """
    start = int(settings.dream_hour_start) % 24
    end = int(settings.dream_hour_end) % 24
    if start == end:
        return False
    hour = datetime.fromtimestamp(now).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# ---------------------------------------------------------------------------
# HostScheduler
# ---------------------------------------------------------------------------


class HostScheduler:
    """Background cron + heartbeat injector for :class:`AgentHost`.

    The scheduler does not know about channels or the SDK. It just appends
    :class:`InboundMessage` to the host's queue under the shared lock. All
    retry / ACL / prompt-wrap logic lives in the normal inbound pipeline.
    """

    def __init__(
        self,
        *,
        agents_dir: Path,
        msg_queue: list[InboundMessage],
        q_lock: threading.Lock,
        stop_event: threading.Event,
    ) -> None:
        self._agents_dir = agents_dir
        self._msg_queue = msg_queue
        self._q_lock = q_lock
        self._stop_event = stop_event
        self._thread: threading.Thread | None = None
        self._heartbeat_state: dict[str, _HeartbeatState] = {}
        # File I/O needs its own lock so add/remove/update from the MCP thread
        # doesn't collide with the ticker thread re-reading jobs.
        self._io_lock = threading.Lock()
        # Coalescing: set of "pending keys" — job_ids (cron) or agent_ids
        # (heartbeat) whose previous payload is still sitting in the host
        # queue or executing inside ``process_inbound``. While a key is in
        # here we refuse to enqueue another copy. ``ack()`` drains it once
        # the host is done. Without this, an "every 30s" cron whose turn
        # takes 40s builds an unbounded backlog — observed live with a
        # heartbeat-test job where user messages were starved behind ~10
        # piled-up cron payloads. See :meth:`ack` and the regression test
        # ``TestCronCoalescing``.
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()

        # Dream (L2 consolidate + L3 axiom distillation) guard. A Dream
        # pass runs two back-to-back LLM calls; end-to-end it can take
        # 30–90 s. We run it on a one-shot worker thread to avoid blocking
        # the 5 s tick loop, and track which agents have an in-flight
        # Dream so ticks during that window don't double-fire. The state
        # is intentionally in-memory: on process restart we start fresh,
        # and the ``dream_hour_start ≤ hour < dream_hour_end`` window
        # combined with the state-file ``last_dream_at`` gate prevents
        # duplicate runs even across restarts.
        self._dream_running: set[str] = set()
        self._dream_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="host-scheduler", daemon=True,
        )
        self._thread.start()
        log.info("HostScheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    # -- public MCP-facing API ----------------------------------------------

    def add_job(
        self,
        *,
        name: str,
        schedule_kind: str,
        schedule_config: dict[str, Any],
        message: str,
        channel: str,
        peer_id: str,
        sender_id: str,
        agent_id: str,
    ) -> str:
        if not name:
            return "Error: 'name' is required."
        if not message:
            return "Error: 'message' is required."
        if not agent_id:
            return "Error: 'agent_id' is required (no active agent in context)."
        err = _validate_schedule(schedule_kind, schedule_config)
        if err:
            return f"Error: {err}"

        now = time.time()
        fire_at = _next_fire_at(schedule_kind, schedule_config, now=now)
        if fire_at is None:
            return "Error: schedule resolves to no future fire time."

        job = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "enabled": True,
            "schedule_kind": schedule_kind,
            "schedule_config": schedule_config,
            "message": message,
            "channel": channel or "cli",
            "peer_id": peer_id or "cli-user",
            "sender_id": sender_id,
            "agent_id": agent_id,
            "created_at": now,
            "next_fire_at": fire_at,
            "last_fire_at": 0,
            # Tracked by ``_finalize_tracked``. Bumps on run failure, resets
            # on success, auto-disables at ``_CRON_AUTO_DISABLE_THRESHOLD``.
            "consecutive_errors": 0,
        }
        with self._io_lock:
            jobs = self._load_jobs(agent_id)
            jobs.append(job)
            self._save_jobs(agent_id, jobs)
        return f"Scheduled '{name}' (id={job['id']}, fires at {self._fmt(fire_at)})."

    def remove_job(self, job_id: str) -> str:
        if not job_id:
            return "Error: 'job_id' is required."
        with self._io_lock:
            for agent_dir in self._iter_agent_dirs():
                jobs = self._load_jobs(agent_dir.name)
                new_jobs = [j for j in jobs if j.get("id") != job_id]
                if len(new_jobs) != len(jobs):
                    self._save_jobs(agent_dir.name, new_jobs)
                    return f"Removed job {job_id}."
        return f"Job {job_id} not found."

    def update_job(self, job_id: str, **updates: Any) -> str:
        if not job_id:
            return "Error: 'job_id' is required."

        fields = {
            k: v for k, v in updates.items()
            if k in {"enabled", "name", "schedule_kind", "schedule_config", "message"}
        }
        if not fields:
            return "Nothing to update."

        with self._io_lock:
            for agent_dir in self._iter_agent_dirs():
                jobs = self._load_jobs(agent_dir.name)
                for job in jobs:
                    if job.get("id") != job_id:
                        continue
                    new_kind = fields.get("schedule_kind", job["schedule_kind"])
                    new_cfg = fields.get("schedule_config", job["schedule_config"])
                    if "schedule_kind" in fields or "schedule_config" in fields:
                        err = _validate_schedule(new_kind, new_cfg)
                        if err:
                            return f"Error: {err}"
                        fire_at = _next_fire_at(new_kind, new_cfg, now=time.time())
                        if fire_at is None:
                            return "Error: schedule resolves to no future fire time."
                        job["next_fire_at"] = fire_at
                    job.update(fields)
                    self._save_jobs(agent_dir.name, jobs)
                    return f"Updated job {job_id}."
        return f"Job {job_id} not found."

    def list_jobs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._io_lock:
            for agent_dir in self._iter_agent_dirs():
                out.extend(self._load_jobs(agent_dir.name))
        return out

    @contextmanager
    def track(
        self, inbound: InboundMessage,
    ) -> Iterator["_TrackedInbound"]:
        """Context manager that owns the scheduler-side bookkeeping for one
        inbound.

        Replaces the old ``ack(source_job_id)`` API. The old design put a
        contract on every caller — "you must call ack in a finally block
        for every scheduler-injected inbound, otherwise the coalesce key
        leaks and that cron job silently stops firing forever" — which is
        exactly the kind of implicit coupling that breaks under later
        refactors. Here the guarantee is enforced by Python: exit the
        ``with`` block (normally or via exception) and the key is released.

        The yielded handle lets the caller mark a *logical* failure even
        when no exception was raised (e.g. ``QueryResult.error`` is set
        but ``run_query`` returned normally). That failure signal drives
        the cron auto-disable counter — see
        :meth:`_finalize_tracked` and
        :data:`_CRON_AUTO_DISABLE_THRESHOLD`.

        Usage (from :meth:`AgentHost.process_inbound`)::

            with self._scheduler.track(inbound) as tracked:
                result = await run_query(...)
                if result.error:
                    tracked.failure(result.error)
                self._dispatch_reply(...)

        No-op for inbounds with empty ``source_job_id`` (user / channel
        messages). Safe to use around every inbound unconditionally.
        """
        tracked = _TrackedInbound(inbound=inbound)
        try:
            yield tracked
        except BaseException as exc:
            # An uncaught exception mid-turn is also a cron failure. Record
            # it before the finally, then re-raise so the caller's own
            # error handling still fires.
            tracked.failure(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._finalize_tracked(tracked)

    def _finalize_tracked(self, tracked: "_TrackedInbound") -> None:
        """Release coalesce key + update cron error counter on ``with`` exit."""
        inbound = tracked.inbound
        key = inbound.source_job_id
        if not key:
            return
        with self._pending_lock:
            self._pending.discard(key)
        if key.startswith("cron:") and inbound.agent_id:
            job_id = key[len("cron:") :]
            self._record_cron_outcome(
                agent_id=inbound.agent_id,
                job_id=job_id,
                failed=tracked.failed,
                error=tracked.error,
            )

    def _record_cron_outcome(
        self,
        *,
        agent_id: str,
        job_id: str,
        failed: bool,
        error: str,
    ) -> None:
        """Bump / reset ``consecutive_errors``; auto-disable past threshold.

        Called from inside ``track()``'s finally. Heartbeats do not go
        through here — they have no "disable" state beyond removing the
        ``HEARTBEAT.md`` file.
        """
        with self._io_lock:
            jobs = self._load_jobs(agent_id)
            dirty = False
            for job in jobs:
                if job.get("id") != job_id:
                    continue
                errs = int(job.get("consecutive_errors", 0) or 0)
                if failed:
                    errs += 1
                    job["consecutive_errors"] = errs
                    if errs >= _CRON_AUTO_DISABLE_THRESHOLD:
                        job["enabled"] = False
                        log.warning(
                            "Cron job '%s' (id=%s) auto-disabled after "
                            "%d consecutive errors: %s",
                            job.get("name", ""), job_id, errs, error or "(no message)",
                        )
                elif errs:
                    job["consecutive_errors"] = 0
                else:
                    # No change needed — don't rewrite the file.
                    break
                dirty = True
                break
            if dirty:
                self._save_jobs(agent_id, jobs)

    # -- internals -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick(time.time())
            except Exception:
                log.exception("HostScheduler tick crashed; continuing")
            # ``wait`` returns True when the event is set -> clean exit.
            if self._stop_event.wait(_TICK_SECONDS):
                break
        log.info("HostScheduler stopped")

    def _tick(self, now: float) -> None:
        for agent_dir in self._iter_agent_dirs():
            self._tick_cron(agent_dir, now)
            self._tick_heartbeat(agent_dir, now)
            self._tick_dream(agent_dir, now)

    def _tick_dream(self, agent_dir: Path, now: float) -> None:
        """Fire a Dream pass for this agent iff all trigger gates are met.

        Gates (documented in ``docs/sdk-contract-notes.md`` §11 and mirrored
        in ``config.Settings``):

        1. Local clock within the ``[dream_hour_start, dream_hour_end)``
           window. ``start == end`` disables Dream entirely.
        2. No other Dream worker is currently running for this agent (the
           in-memory ``_dream_running`` guard).
        3. ``state.json`` has NOT been stamped with a ``last_dream_at`` in
           the current window — prevents a process restart inside the
           idle window from running Dream twice on the same night.
        4. Observation count ≥ ``dream_min_observations`` — don't
           consolidate over near-empty input.
        5. Last agent activity ≥ ``dream_inactive_minutes`` ago — don't
           collide with an active conversation.

        When all five pass, we spawn a one-shot worker thread (see
        :meth:`_run_dream`) so the 5 s scheduler tick keeps flowing. Any
        cron / heartbeat ticks during the Dream run are unaffected.
        """
        agent_id = agent_dir.name

        if not _in_dream_window(now):
            return

        with self._dream_lock:
            if agent_id in self._dream_running:
                return

        # Lazy import: keeps ``memory`` out of the import path for callers
        # who only want the scheduler (tests for cron coalescing, etc.).
        try:
            from pip_agent.memory import MemoryStore
        except Exception:  # pragma: no cover — memory package is bundled
            log.exception("Dream: memory package import failed")
            return

        try:
            store = MemoryStore(base_dir=self._agents_dir, agent_id=agent_id)
            state = store.load_state()
        except Exception:
            log.exception("Dream: cannot load state for agent=%s", agent_id)
            return

        last_dream_at = float(state.get(_DREAM_LAST_AT_KEY) or 0.0)
        # Same-window re-entry guard: if we already ran Dream since the
        # current window began, skip. We approximate "current window began"
        # as "now minus (dream_hour_end - dream_hour_start) hours" — a
        # coarse but conservative bound that correctly handles both
        # simple and wrap-around windows.
        start_h = int(settings.dream_hour_start) % 24
        end_h = int(settings.dream_hour_end) % 24
        span_h = (end_h - start_h) % 24 or 24
        window_floor = now - span_h * 3600
        if last_dream_at >= window_floor:
            return

        min_obs = int(settings.dream_min_observations)
        if min_obs > 0:
            try:
                observations = store.load_all_observations()
            except Exception:
                log.exception(
                    "Dream: cannot load observations for agent=%s", agent_id,
                )
                return
            if len(observations) < min_obs:
                return
        else:
            observations = None  # worker will reload

        idle_min = int(settings.dream_inactive_minutes)
        if idle_min > 0:
            last_activity = float(state.get(_LAST_ACTIVITY_KEY) or 0.0)
            if last_activity > 0 and (now - last_activity) < idle_min * 60:
                return

        with self._dream_lock:
            if agent_id in self._dream_running:
                # Re-check under the lock — another tick could have
                # spawned in the narrow gap between our first check and
                # here. Cheap to repeat; catastrophic to skip.
                return
            self._dream_running.add(agent_id)

        log.info(
            "Dream: triggering for agent=%s (obs>=%d, idle>=%dmin, window=%d-%d)",
            agent_id, min_obs, idle_min, start_h, end_h,
        )
        threading.Thread(
            target=self._run_dream,
            args=(agent_id, now, observations),
            name=f"dream-{agent_id}",
            daemon=True,
        ).start()

    def _run_dream(
        self,
        agent_id: str,
        started_at: float,
        observations: list | None,
    ) -> None:
        """Worker thread body for a single Dream pass.

        Blocks on two LLM calls. Always clears the ``_dream_running``
        guard on exit, even on exception — a crashed Dream must not
        deadlock the next night's window.
        """
        try:
            from pip_agent.anthropic_client import build_anthropic_client
            from pip_agent.memory import MemoryStore
            from pip_agent.memory.consolidate import consolidate, distill_axioms

            client = build_anthropic_client()
            if client is None:
                log.info(
                    "Dream: skipped for agent=%s — no Anthropic credentials",
                    agent_id,
                )
                return

            store = MemoryStore(base_dir=self._agents_dir, agent_id=agent_id)
            if observations is None:
                observations = store.load_all_observations()
            memories = store.load_memories()

            state = store.load_state()
            cycle = int(state.get(_DREAM_CYCLE_KEY) or 0) + 1

            new_memories = consolidate(
                client, observations, memories, cycle_count=cycle,
            )
            store.save_memories(new_memories)

            axioms_text = distill_axioms(client, new_memories)
            if axioms_text:
                store.save_axioms(axioms_text)

            # Stamp AFTER persistence so a crash between consolidate and
            # the state write doesn't block the next tick — better to
            # double-run than to silently drop observations on the floor.
            state[_DREAM_LAST_AT_KEY] = time.time()
            state[_DREAM_CYCLE_KEY] = cycle
            store.save_state(state)

            log.info(
                "Dream: done for agent=%s cycle=%d obs=%d mem=%d axioms=%s "
                "duration=%.1fs",
                agent_id, cycle, len(observations), len(new_memories),
                "yes" if axioms_text else "no",
                time.time() - started_at,
            )
        except Exception:
            log.exception("Dream: crashed for agent=%s", agent_id)
        finally:
            with self._dream_lock:
                self._dream_running.discard(agent_id)

    def _tick_cron(self, agent_dir: Path, now: float) -> None:
        agent_id = agent_dir.name
        with self._io_lock:
            jobs = self._load_jobs(agent_id)
        if not jobs:
            return

        dirty = False
        for job in jobs:
            if not job.get("enabled"):
                continue
            fire_at = float(job.get("next_fire_at") or 0)
            if fire_at <= 0 or fire_at > now:
                continue

            jid = str(job.get("id") or "")
            pending_key = _pending_key_cron(jid)
            with self._pending_lock:
                coalesced = pending_key in self._pending
                if not coalesced:
                    self._pending.add(pending_key)

            if coalesced:
                # Previous run of this job is still in the host queue or
                # mid-process. Don't stack another copy — that's what let
                # user messages get buried behind N cron payloads. Still
                # advance ``next_fire_at`` below so the same tick doesn't
                # retry this branch every 5 s; effectively we're dropping
                # this interval rather than reshuffling the schedule.
                log.warning(
                    "HostScheduler coalesced cron '%s' (id=%s) for agent=%s "
                    "— previous run still pending (queue depth=%d)",
                    job.get("name", ""), jid, agent_id, len(self._msg_queue),
                )
            else:
                self._enqueue(
                    InboundMessage(
                        text=job.get("message", ""),
                        sender_id=_Sender.CRON,
                        channel=job.get("channel") or "cli",
                        peer_id=job.get("peer_id") or "cli-user",
                        agent_id=agent_id,
                        source_job_id=pending_key,
                    )
                )
            job["last_fire_at"] = now
            kind = job.get("schedule_kind", "")
            if kind == "at":
                job["enabled"] = False
                job["next_fire_at"] = 0
            else:
                nxt = _next_fire_at(kind, job.get("schedule_config", {}), now=now)
                if nxt is None:
                    job["enabled"] = False
                    job["next_fire_at"] = 0
                else:
                    job["next_fire_at"] = nxt
            dirty = True

        if dirty:
            with self._io_lock:
                self._save_jobs(agent_id, jobs)

    def _tick_heartbeat(self, agent_dir: Path, now: float) -> None:
        hb_file = agent_dir / _HEARTBEAT_FILE
        if not hb_file.is_file():
            return
        interval = int(settings.heartbeat_interval)
        if interval <= 0:
            return
        if not _in_active_window(now):
            return

        state = self._heartbeat_state.setdefault(
            agent_dir.name, _HeartbeatState(last_fire_at=now)
        )
        if now - state.last_fire_at < interval:
            return

        agent_id = agent_dir.name
        pending_key = _pending_key_heartbeat(agent_id)
        with self._pending_lock:
            if pending_key in self._pending:
                # Same coalescing story as cron: previous heartbeat hasn't
                # been ack'd yet. Skip this tick but advance ``last_fire_at``
                # so we don't hammer the check every 5 s.
                log.warning(
                    "HostScheduler coalesced heartbeat for agent=%s "
                    "— previous run still pending (queue depth=%d)",
                    agent_id, len(self._msg_queue),
                )
                state.last_fire_at = now
                return
            self._pending.add(pending_key)

        try:
            payload = hb_file.read_text("utf-8").strip()
        except OSError as exc:
            log.warning("Cannot read %s: %s", hb_file, exc)
            # Release the pending slot we just reserved; this heartbeat
            # will never be ack'd otherwise.
            with self._pending_lock:
                self._pending.discard(pending_key)
            return
        if not payload:
            with self._pending_lock:
                self._pending.discard(pending_key)
            return

        self._enqueue(
            InboundMessage(
                text=payload,
                sender_id=_Sender.HEARTBEAT,
                channel="cli",
                peer_id="cli-user",
                agent_id=agent_id,
                source_job_id=pending_key,
            )
        )
        state.last_fire_at = now

    def _enqueue(self, inbound: InboundMessage) -> None:
        with self._q_lock:
            self._msg_queue.append(inbound)
            depth = len(self._msg_queue)
        log.info(
            "HostScheduler enqueued %s for agent=%s (queue depth=%d, key=%s)",
            inbound.sender_id, inbound.agent_id, depth, inbound.source_job_id,
        )

    def _iter_agent_dirs(self) -> list[Path]:
        if not self._agents_dir.is_dir():
            return []
        return [p for p in self._agents_dir.iterdir() if p.is_dir()]

    def _cron_path(self, agent_id: str) -> Path:
        return self._agents_dir / agent_id / _CRON_FILE

    def _load_jobs(self, agent_id: str) -> list[dict[str, Any]]:
        path = self._cron_path(agent_id)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Corrupt cron.json at %s: %s — ignoring", path, exc)
            return []
        if not isinstance(data, list):
            return []
        return [j for j in data if isinstance(j, dict) and j.get("id")]

    def _save_jobs(self, agent_id: str, jobs: list[dict[str, Any]]) -> None:
        path = self._cron_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(jobs, indent=2, ensure_ascii=False))

    @staticmethod
    def _fmt(ts: float) -> str:
        if ts <= 0:
            return "never"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
