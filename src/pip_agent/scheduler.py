"""Unified background scheduler for all periodic tasks.

Manages reflect, dream, heartbeat, and user-defined cron jobs. Each job is
routed to a named lane in a shared :class:`CommandQueue`; lanes run
independently so jobs never starve each other. The scheduler itself only
polls for due jobs and dispatches them; actual execution happens on lane
worker threads.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from pip_agent.lanes import CommandQueue

if TYPE_CHECKING:
    import anthropic

    from pip_agent.memory import MemoryStore

log = logging.getLogger(__name__)

TICK_INTERVAL = 10
CRON_AUTO_DISABLE_THRESHOLD = 5

# Standard lane names used by built-in background jobs.
LANE_REFLECT = "reflect"
LANE_DREAM = "dream"
LANE_HEARTBEAT = "heartbeat"
LANE_CRON = "cron"

_ID_RE = re.compile(r"[^a-z0-9_-]+")


def _slug(name: str) -> str:
    return _ID_RE.sub("-", name.strip().lower()).strip("-")[:64] or "job"


# ---------------------------------------------------------------------------
# BackgroundJob ABC
# ---------------------------------------------------------------------------

class BackgroundJob(ABC):
    """Base class for all scheduler jobs.

    ``lane_name`` selects which :class:`~pip_agent.lanes.LaneQueue` the job
    runs on. Jobs on different lanes run in parallel; jobs on the same lane
    are serialized in FIFO order.
    """

    name: str = "job"
    lane_name: str = "default"

    @abstractmethod
    def should_run(self, now: float) -> tuple[bool, str]:
        """Return (should_run, reason)."""
        ...

    @abstractmethod
    def execute(self, now: float, output_queue: list[str], queue_lock: threading.Lock) -> None:
        ...


# ---------------------------------------------------------------------------
# ReflectJob — L1 memory reflection
# ---------------------------------------------------------------------------

class ReflectJob(BackgroundJob):
    name = "reflect"
    lane_name = LANE_REFLECT

    def __init__(
        self,
        memory_stores: dict[str, MemoryStore],
        client: anthropic.Anthropic,
        *,
        model: str = "",
    ) -> None:
        self.stores = memory_stores
        self.client = client
        self.model = model

    def should_run(self, now: float) -> tuple[bool, str]:
        from pip_agent.config import settings

        total = 0
        for store in list(self.stores.values()):
            total += self._count_new_transcripts_for(store)
        threshold = settings.reflect_transcript_threshold
        if total < threshold:
            return False, f"transcripts {total}/{threshold}"
        return True, "transcript threshold reached"

    def execute(self, now: float, output_queue: list[str], queue_lock: threading.Lock) -> None:
        from pip_agent.memory.reflect import reflect

        for agent_id, store in list(self.stores.items()):
            transcripts_dir = store.agent_dir / "transcripts"
            if not transcripts_dir.is_dir():
                continue

            state = store.load_state()
            since = state.get("last_reflect_transcript_ts", 0)

            if self._count_new_transcripts_for(store) == 0:
                continue

            observations = reflect(
                self.client,
                transcripts_dir,
                agent_id,
                since,
                model=self.model,
            )

            if observations:
                store.write_observations(observations)
                log.info(
                    "L1 reflect: %d observations for agent %s",
                    len(observations), agent_id,
                )

            latest = self._latest_transcript_ts(transcripts_dir)
            if latest > 0:
                state["last_reflect_transcript_ts"] = latest
            state["last_reflect_at"] = now
            store.save_state(state)

            self._cleanup_transcripts(store, transcripts_dir, state, now)

    @staticmethod
    def _count_new_transcripts_for(store: MemoryStore) -> int:
        transcripts_dir = store.agent_dir / "transcripts"
        if not transcripts_dir.is_dir():
            return 0
        state = store.load_state()
        last_ts = state.get("last_reflect_transcript_ts", 0)
        count = 0
        for fp in transcripts_dir.glob("*.json"):
            try:
                ts = int(fp.stem)
            except ValueError:
                continue
            if ts > last_ts:
                count += 1
        return count

    @staticmethod
    def _latest_transcript_ts(transcripts_dir: Path) -> int:
        if not transcripts_dir.is_dir():
            return 0
        latest = 0
        for fp in transcripts_dir.glob("*.json"):
            try:
                ts = int(fp.stem)
            except ValueError:
                continue
            if ts > latest:
                latest = ts
        return latest

    @staticmethod
    def _cleanup_transcripts(
        store: MemoryStore, transcripts_dir: Path, state: dict, now: float,
    ) -> None:
        if not transcripts_dir.is_dir():
            return
        from pip_agent.config import settings

        cutoff = now - settings.transcript_retention_days * 86400
        last_reflected_ts = state.get("last_reflect_transcript_ts", 0)
        removed = 0
        for fp in transcripts_dir.glob("*.json"):
            try:
                ts = int(fp.stem)
            except ValueError:
                continue
            if ts < cutoff and ts <= last_reflected_ts:
                fp.unlink(missing_ok=True)
                removed += 1
        if removed:
            log.info(
                "Transcript cleanup: removed %d old files for agent %s",
                removed, store.agent_id,
            )


# ---------------------------------------------------------------------------
# DreamJob — L2 Consolidate + L3 Axioms
# ---------------------------------------------------------------------------

class DreamJob(BackgroundJob):
    name = "dream"
    lane_name = LANE_DREAM

    def __init__(
        self,
        memory_stores: dict[str, MemoryStore],
        client: anthropic.Anthropic,
        *,
        model: str = "",
    ) -> None:
        self.stores = memory_stores
        self.client = client
        self.model = model

    def should_run(self, now: float) -> tuple[bool, str]:
        from pip_agent.config import settings

        local_now = datetime.fromtimestamp(now)

        if local_now.hour != settings.dream_hour:
            return False, f"not dream hour (current={local_now.hour}, target={settings.dream_hour})"

        for store in list(self.stores.values()):
            state = store.load_state()
            last_dream = state.get("last_dream_at", 0)
            if last_dream > 0:
                last_dream_date = datetime.fromtimestamp(last_dream).date()
                if last_dream_date == local_now.date():
                    continue

            obs_count = len(store.load_all_observations())
            if obs_count < settings.dream_min_observations:
                continue

            last_activity = state.get("last_activity_at", 0)
            if last_activity > 0 and (now - last_activity) < settings.dream_inactive_minutes * 60:
                continue

            return True, f"dream conditions met for agent {store.agent_id}"

        return False, "no agent ready to dream"

    def execute(self, now: float, output_queue: list[str], queue_lock: threading.Lock) -> None:
        from pip_agent.config import settings
        from pip_agent.memory.consolidate import consolidate, distill_axioms

        local_now = datetime.fromtimestamp(now)

        for agent_id, store in list(self.stores.items()):
            state = store.load_state()
            last_dream = state.get("last_dream_at", 0)
            if last_dream > 0:
                last_dream_date = datetime.fromtimestamp(last_dream).date()
                if last_dream_date == local_now.date():
                    continue

            observations = store.load_all_observations()
            if len(observations) < settings.dream_min_observations:
                continue

            last_activity = state.get("last_activity_at", 0)
            if last_activity > 0 and (now - last_activity) < settings.dream_inactive_minutes * 60:
                continue

            memories = store.load_memories()
            cycle = state.get("consolidate_cycle", 0) + 1

            updated = consolidate(
                self.client, observations, memories, cycle, model=self.model,
            )
            store.save_memories(updated)

            axioms_text = distill_axioms(self.client, updated, model=self.model)
            if axioms_text:
                store.save_axioms(axioms_text)

            cleared = store.clear_observations()

            state["last_dream_at"] = now
            state["consolidate_cycle"] = cycle
            store.save_state(state)

            log.info(
                "Dream complete: %d memories, axioms=%s, cleared %d obs files for agent %s",
                len(updated), bool(axioms_text), cleared, agent_id,
            )


# ---------------------------------------------------------------------------
# HeartbeatJob
# ---------------------------------------------------------------------------

HEARTBEAT_SENDER = "__heartbeat__"


class HeartbeatJob(BackgroundJob):
    """Heartbeat job that enqueues synthetic messages into the main loop.

    Scans all agent directories under ``agents_dir`` for HEARTBEAT.md files.
    Each agent with a valid heartbeat config gets its own message enqueued
    through the normal agent_loop pipeline, giving every agent full tool
    access, memory enrichment, and conversation management.
    """

    name = "heartbeat"
    lane_name = LANE_HEARTBEAT

    def __init__(
        self,
        agents_dir: Path,
        *,
        msg_queue: list | None = None,
        q_lock: threading.Lock | None = None,
    ) -> None:
        self.agents_dir = agents_dir
        self.msg_queue = msg_queue
        self.q_lock = q_lock
        self.last_run_at: float = time.time()

    _FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)

    def _parse_heartbeat(self, heartbeat_path: Path) -> tuple[dict, str]:
        """Read HEARTBEAT.md and split YAML frontmatter from body."""
        raw = heartbeat_path.read_text(encoding="utf-8", errors="replace").strip()
        m = self._FM_RE.match(raw)
        if not m:
            return {}, raw
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, m.group(2).strip()

    def _iter_heartbeats(self) -> list[Path]:
        """Return all HEARTBEAT.md files across agent directories."""
        results: list[Path] = []
        if not self.agents_dir.is_dir():
            return results
        for child in self.agents_dir.iterdir():
            if not child.is_dir():
                continue
            hb = child / "HEARTBEAT.md"
            if hb.is_file():
                results.append(hb)
        return results

    def should_run(self, now: float) -> tuple[bool, str]:
        from pip_agent.config import settings

        heartbeats = self._iter_heartbeats()
        if not heartbeats:
            return False, "no HEARTBEAT.md found"

        elapsed = now - self.last_run_at
        if elapsed < settings.heartbeat_interval:
            remaining = settings.heartbeat_interval - elapsed
            return False, f"interval not elapsed ({remaining:.0f}s remaining)"

        hour = datetime.fromtimestamp(now).hour
        s, e = settings.heartbeat_active_start, settings.heartbeat_active_end
        in_hours = (s <= hour < e) if s <= e else not (e <= hour < s)
        if not in_hours:
            return False, f"outside active hours ({s}:00-{e}:00)"

        for hb_path in heartbeats:
            try:
                _meta, body = self._parse_heartbeat(hb_path)
            except OSError:
                continue
            if body:
                return True, "all checks passed"

        return False, "all HEARTBEAT.md files are empty"

    def execute(self, now: float, output_queue: list[str], queue_lock: threading.Lock) -> None:
        for hb_path in self._iter_heartbeats():
            try:
                meta, instructions = self._parse_heartbeat(hb_path)
            except OSError:
                continue
            if not instructions:
                continue
            agent_id = hb_path.parent.name
            channel = meta.get("channel", "cli")
            peer_id = meta.get("peer_id", "cli-user")
            self._enqueue(
                f"<heartbeat>\n{instructions}\n</heartbeat>",
                channel=channel, peer_id=peer_id,
                agent_id=agent_id,
            )
            log.debug(
                "Heartbeat enqueued for %s (channel=%s, peer_id=%s)",
                agent_id, channel, peer_id,
            )
        self.last_run_at = time.time()

    def trigger(self) -> str:
        """Manual trigger, bypasses interval check."""
        heartbeats = self._iter_heartbeats()
        if not heartbeats:
            return "No HEARTBEAT.md found"
        enqueued = 0
        for hb_path in heartbeats:
            try:
                meta, instructions = self._parse_heartbeat(hb_path)
            except OSError:
                continue
            if not instructions:
                continue
            agent_id = hb_path.parent.name
            channel = meta.get("channel", "cli")
            peer_id = meta.get("peer_id", "cli-user")
            self._enqueue(
                f"<heartbeat>\n{instructions}\n</heartbeat>",
                channel=channel, peer_id=peer_id,
                agent_id=agent_id,
            )
            enqueued += 1
        self.last_run_at = time.time()
        return f"heartbeat enqueued for {enqueued} agent(s)"

    def _enqueue(
        self,
        text: str,
        channel: str = "cli",
        peer_id: str = "cli-user",
        agent_id: str = "",
    ) -> None:
        if self.msg_queue is None or self.q_lock is None:
            log.warning("HeartbeatJob: msg_queue not configured, cannot enqueue")
            return
        from pip_agent.channels import InboundMessage

        msg = InboundMessage(
            text=text,
            sender_id=HEARTBEAT_SENDER,
            channel=channel,
            peer_id=peer_id,
            agent_id=agent_id,
        )
        with self.q_lock:
            self.msg_queue.append(msg)

    def status(self) -> dict[str, Any]:
        from pip_agent.config import settings

        now = time.time()
        elapsed = now - self.last_run_at if self.last_run_at > 0 else None
        if elapsed is not None:
            next_in = max(0.0, settings.heartbeat_interval - elapsed)
        else:
            next_in = settings.heartbeat_interval
        ok, reason = self.should_run(now)
        hb_count = len(self._iter_heartbeats())
        return {
            "enabled": hb_count > 0,
            "agents": hb_count,
            "should_run": ok,
            "reason": reason,
            "last_run": (
                datetime.fromtimestamp(self.last_run_at).isoformat()
                if self.last_run_at > 0 else "never"
            ),
            "next_in": f"{round(next_in)}s",
            "interval": f"{settings.heartbeat_interval}s",
            "active_hours": (
                f"{settings.heartbeat_active_start}:00"
                f"-{settings.heartbeat_active_end}:00"
            ),
        }


# ---------------------------------------------------------------------------
# CronJob + CronService
# ---------------------------------------------------------------------------

CRON_SENDER = "__cron__"


@dataclass
class CronJobSource:
    """Where this cron job was created — used to route output back."""
    channel: str = "cli"
    peer_id: str = "cli-user"
    sender_id: str = ""


@dataclass
class CronJob:
    id: str
    name: str
    enabled: bool
    schedule_kind: str       # "at" | "every" | "cron"
    schedule_config: dict
    payload: dict
    source: CronJobSource = field(default_factory=CronJobSource)
    delete_after_run: bool = False
    consecutive_errors: int = 0
    last_run_at: float = 0.0
    next_run_at: float = 0.0


class CronService(BackgroundJob):
    """Manages user-defined scheduled tasks from CRON.json.

    Scans all agent directories under ``agents_dir`` for CRON.json files.
    Instead of calling LLM directly, enqueues synthetic InboundMessages
    into the shared msg_queue for processing by the main agent loop.
    """

    name = "cron"
    lane_name = LANE_CRON

    def __init__(
        self,
        agents_dir: Path,
        *,
        msg_queue: list | None = None,
        q_lock: threading.Lock | None = None,
    ) -> None:
        self.agents_dir = agents_dir
        self.msg_queue = msg_queue
        self.q_lock = q_lock
        self._lock = threading.Lock()
        self.jobs: list[CronJob] = []
        self._cron_files: dict[str, Path] = {}
        self.load_jobs()

    # -- persistence --

    def _iter_cron_files(self) -> list[Path]:
        """Return all CRON.json files across agent directories."""
        results: list[Path] = []
        if not self.agents_dir.is_dir():
            return results
        for child in self.agents_dir.iterdir():
            if not child.is_dir():
                continue
            cf = child / "CRON.json"
            if cf.is_file():
                results.append(cf)
        return results

    def load_jobs(self) -> None:
        self.jobs.clear()
        self._cron_files.clear()
        now = time.time()
        for cron_file in self._iter_cron_files():
            self._load_from_file(cron_file, now)

    def _load_from_file(self, cron_file: Path, now: float) -> None:
        try:
            raw = json.loads(cron_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.warning("CRON.json load error (%s): %s", cron_file, exc)
            return
        agent_prefix = cron_file.parent.name
        for jd in raw.get("jobs", []):
            sched = jd.get("schedule", {})
            kind = sched.get("kind", "")
            if kind not in ("at", "every", "cron"):
                continue
            src = jd.get("source", {})
            raw_id = jd.get("id", _slug(jd.get("name", "")))
            qualified_id = (
                raw_id if raw_id.startswith(f"{agent_prefix}/")
                else f"{agent_prefix}/{raw_id}"
            )
            job = CronJob(
                id=qualified_id,
                name=jd.get("name", ""),
                enabled=jd.get("enabled", True),
                schedule_kind=kind,
                schedule_config=sched,
                payload=jd.get("payload", {}),
                source=CronJobSource(
                    channel=src.get("channel", "cli"),
                    peer_id=src.get("peer_id", "cli-user"),
                    sender_id=src.get("sender_id", ""),
                ),
                delete_after_run=jd.get("delete_after_run", False),
                consecutive_errors=jd.get("consecutive_errors", 0),
            )
            job.next_run_at = self._compute_next(job, now)
            self.jobs.append(job)
            self._cron_files[job.id] = cron_file

    @property
    def cron_file(self) -> Path:
        """Default cron file path (first agent dir) for backward compat."""
        if self._cron_files:
            return next(iter(self._cron_files.values()))
        return self.agents_dir / "CRON.json"

    def _save_jobs(self) -> None:
        from pip_agent.fileutil import atomic_write

        by_file: dict[Path, list[CronJob]] = {}
        for job in self.jobs:
            cf = self._cron_files.get(job.id, self.cron_file)
            by_file.setdefault(cf, []).append(job)

        for cron_file, jobs in by_file.items():
            data = {
                "jobs": [
                    {
                        "id": j.id, "name": j.name, "enabled": j.enabled,
                        "schedule": {
                            "kind": j.schedule_kind, **j.schedule_config,
                        },
                        "payload": j.payload,
                        "source": {
                            "channel": j.source.channel,
                            "peer_id": j.source.peer_id,
                            "sender_id": j.source.sender_id,
                        },
                        "delete_after_run": j.delete_after_run,
                        "consecutive_errors": j.consecutive_errors,
                    }
                    for j in jobs
                ],
            }
            atomic_write(cron_file, json.dumps(data, indent=2, ensure_ascii=False))

    # -- scheduling --

    def _compute_next(self, job: CronJob, now: float) -> float:
        cfg = job.schedule_config
        if job.schedule_kind == "at":
            try:
                ts = datetime.fromisoformat(cfg.get("at", "")).timestamp()
                return ts if ts > now else 0.0
            except (ValueError, OSError):
                return 0.0
        if job.schedule_kind == "every":
            every = cfg.get("every_seconds", 3600)
            try:
                anchor = datetime.fromisoformat(cfg.get("anchor", "")).timestamp()
            except (ValueError, OSError, TypeError):
                anchor = now
            if now < anchor:
                return anchor
            steps = int((now - anchor) / every) + 1
            return anchor + steps * every
        if job.schedule_kind == "cron":
            expr = cfg.get("expr", "")
            if not expr:
                return 0.0
            try:
                from croniter import croniter
                return croniter(expr, datetime.fromtimestamp(now)).get_next(datetime).timestamp()
            except (ValueError, KeyError, ImportError):
                return 0.0
        return 0.0

    # -- BackgroundJob interface --

    def should_run(self, now: float) -> tuple[bool, str]:
        with self._lock:
            for job in self.jobs:
                if job.enabled and job.next_run_at > 0 and now >= job.next_run_at:
                    return True, f"job '{job.name}' is due"
        return False, "no jobs due"

    def execute(self, now: float, output_queue: list[str], queue_lock: threading.Lock) -> None:
        with self._lock:
            remove_ids: list[str] = []
            for job in self.jobs:
                if not job.enabled or job.next_run_at <= 0 or now < job.next_run_at:
                    continue
                self._run_job(job, now)
                if job.delete_after_run and job.schedule_kind == "at":
                    remove_ids.append(job.id)
            if remove_ids:
                self.jobs = [j for j in self.jobs if j.id not in remove_ids]
                self._save_jobs()

    def _run_job(self, job: CronJob, now: float) -> None:
        payload = job.payload
        kind = payload.get("kind", "")
        msg = payload.get("message", "") if kind == "agent_turn" else payload.get("text", "")

        if not msg:
            log.debug("Cron job '%s' skipped: empty message", job.name)
            job.last_run_at = now
            job.next_run_at = self._compute_next(job, now)
            self._save_jobs()
            return

        self._enqueue(job, msg)

        job.last_run_at = now
        job.next_run_at = self._compute_next(job, now)
        self._save_jobs()

        entry = {
            "job_id": job.id,
            "run_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "status": "enqueued",
        }
        cron_file = self._cron_files.get(job.id, self.cron_file)
        run_log = cron_file.parent / "cron-runs.jsonl"
        try:
            with open(run_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

        log.debug(
            "Cron job '%s' enqueued to %s:%s",
            job.name, job.source.channel, job.source.peer_id,
        )

    def _enqueue(self, job: CronJob, message: str) -> None:
        if self.msg_queue is None or self.q_lock is None:
            log.warning("CronService: msg_queue not configured, cannot enqueue")
            return
        from pip_agent.channels import InboundMessage

        text = (
            f'<cron_task job_id="{job.id}" name="{job.name}">\n'
            f"{message}\n"
            f"</cron_task>"
        )
        msg = InboundMessage(
            text=text,
            sender_id=CRON_SENDER,
            channel=job.source.channel,
            peer_id=job.source.peer_id,
        )
        with self.q_lock:
            self.msg_queue.append(msg)

    # -- CRUD for agent tools --

    def add_job(
        self,
        name: str,
        schedule_kind: str,
        schedule_config: dict,
        message: str,
        *,
        delete_after_run: bool | None = None,
        channel: str = "cli",
        peer_id: str = "cli-user",
        sender_id: str = "",
        agent_id: str = "",
    ) -> str:
        if schedule_kind not in ("at", "every", "cron"):
            return f"[error] Invalid schedule_kind: {schedule_kind}"

        with self._lock:
            prefix = agent_id or "default"
            job_id = f"{prefix}/{_slug(name)}"
            base = job_id
            counter = 1
            existing_ids = {j.id for j in self.jobs}
            while job_id in existing_ids:
                counter += 1
                job_id = f"{base}-{counter}"

            auto_delete = (
                delete_after_run
                if delete_after_run is not None
                else (schedule_kind == "at")
            )

            job = CronJob(
                id=job_id, name=name, enabled=True,
                schedule_kind=schedule_kind,
                schedule_config={"kind": schedule_kind, **schedule_config},
                payload={"kind": "agent_turn", "message": message},
                source=CronJobSource(
                    channel=channel, peer_id=peer_id, sender_id=sender_id,
                ),
                delete_after_run=auto_delete,
            )
            if agent_id:
                target_file = self.agents_dir / agent_id / "CRON.json"
            else:
                target_file = self.cron_file
            self._cron_files[job_id] = target_file

            job.next_run_at = self._compute_next(job, time.time())
            self.jobs.append(job)
            self._save_jobs()

        next_str = (
            datetime.fromtimestamp(job.next_run_at).strftime("%Y-%m-%d %H:%M:%S")
            if job.next_run_at > 0 else "n/a"
        )
        return f"Created job '{name}' (id={job_id}, next_run={next_str})"

    def remove_job(self, job_id: str) -> str:
        with self._lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j.id != job_id]
            if len(self.jobs) < before:
                self._save_jobs()
                return f"Removed job '{job_id}'"
        return f"[error] Job '{job_id}' not found"

    def update_job(self, job_id: str, **fields: Any) -> str:
        with self._lock:
            for job in self.jobs:
                if job.id != job_id:
                    continue

                updated: list[str] = []
                if "enabled" in fields:
                    job.enabled = bool(fields["enabled"])
                    updated.append(f"enabled={job.enabled}")
                if "name" in fields:
                    job.name = str(fields["name"])
                    updated.append(f"name={job.name}")
                if "schedule_kind" in fields:
                    kind = str(fields["schedule_kind"])
                    if kind not in ("at", "every", "cron"):
                        return f"[error] Invalid schedule_kind: {kind}"
                    job.schedule_kind = kind
                    updated.append(f"schedule_kind={kind}")
                if "schedule_config" in fields:
                    cfg = fields["schedule_config"]
                    if isinstance(cfg, dict):
                        job.schedule_config = {"kind": job.schedule_kind, **cfg}
                        updated.append("schedule_config updated")
                if "message" in fields:
                    job.payload = {
                        "kind": "agent_turn",
                        "message": str(fields["message"]),
                    }
                    updated.append("message updated")

                if "schedule_kind" in fields or "schedule_config" in fields:
                    job.next_run_at = self._compute_next(job, time.time())
                    job.consecutive_errors = 0

                if not updated:
                    return "No fields to update."

                self._save_jobs()
                return f"Updated job '{job_id}': {', '.join(updated)}"

        return f"[error] Job '{job_id}' not found"

    def list_jobs(self) -> list[dict[str, Any]]:
        now = time.time()
        result = []
        for j in self.jobs:
            nxt = max(0.0, j.next_run_at - now) if j.next_run_at > 0 else None
            result.append({
                "id": j.id, "name": j.name, "enabled": j.enabled,
                "kind": j.schedule_kind, "errors": j.consecutive_errors,
                "last_run": (
                    datetime.fromtimestamp(j.last_run_at).isoformat()
                    if j.last_run_at > 0 else "never"
                ),
                "next_run": (
                    datetime.fromtimestamp(j.next_run_at).isoformat()
                    if j.next_run_at > 0 else "n/a"
                ),
                "next_in": round(nxt) if nxt is not None else None,
            })
        return result

    def trigger_job(self, job_id: str) -> str:
        for job in self.jobs:
            if job.id == job_id:
                self._run_job(job, time.time())
                return f"'{job.name}' enqueued"
        return f"[error] Job '{job_id}' not found"

    def report_outcome(self, job_id: str, *, success: bool) -> None:
        """Called after an enqueued cron message has been processed.

        On success: reset consecutive_errors.
        On failure: increment consecutive_errors; auto-disable if threshold
        is reached.
        """
        for job in self.jobs:
            if job.id != job_id:
                continue
            if success:
                if job.consecutive_errors != 0:
                    job.consecutive_errors = 0
                    self._save_jobs()
            else:
                job.consecutive_errors += 1
                if job.consecutive_errors >= CRON_AUTO_DISABLE_THRESHOLD:
                    job.enabled = False
                    log.warning(
                        "Cron job '%s' auto-disabled after %d consecutive errors",
                        job.name, job.consecutive_errors,
                    )
                self._save_jobs()
            return


# ---------------------------------------------------------------------------
# BackgroundScheduler
# ---------------------------------------------------------------------------

class BackgroundScheduler:
    """Polls registered jobs and dispatches due work to lane queues.

    The scheduler runs a single daemon thread that wakes every
    :data:`TICK_INTERVAL` seconds. For each due job, it checks whether the
    job's lane is busy and, if not, enqueues the job's ``execute`` callable
    into that lane. Actual work runs on the lane's own worker thread, so
    slow jobs never block ``_tick`` or other lanes.
    """

    def __init__(
        self,
        command_queue: CommandQueue,
        stop_event: threading.Event,
    ) -> None:
        self.command_queue = command_queue
        self.stop_event = stop_event
        self._jobs: list[BackgroundJob] = []
        self._output_queue: list[str] = []
        self._queue_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def register(self, job: BackgroundJob) -> None:
        self._jobs.append(job)
        # Pre-create the lane so stats() shows it even before first dispatch.
        self.command_queue.get_or_create_lane(job.lane_name)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="bg-scheduler",
        )
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _loop(self) -> None:
        log.debug("BackgroundScheduler started (%d jobs)", len(self._jobs))
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("BackgroundScheduler tick error")
            self.stop_event.wait(TICK_INTERVAL)
        log.debug("BackgroundScheduler stopped")

    def _tick(self) -> None:
        now = time.time()
        for job in self._jobs:
            if self.stop_event.is_set():
                break
            try:
                ok, _reason = job.should_run(now)
            except Exception:
                log.exception("should_run error for %s", job.name)
                continue
            if not ok:
                continue

            if self.command_queue.lane_busy(job.lane_name):
                log.debug("Lane '%s' busy, skipping %s", job.lane_name, job.name)
                continue

            self._dispatch(job, now)

    def _dispatch(self, job: BackgroundJob, now: float) -> None:
        """Enqueue ``job.execute`` into its lane. Errors are logged, not raised."""

        def _run() -> None:
            try:
                job.execute(now, self._output_queue, self._queue_lock)
            except Exception:
                log.exception("Job '%s' execute error", job.name)

        try:
            self.command_queue.enqueue(job.lane_name, _run)
        except Exception:
            log.exception("Failed to enqueue job '%s' on lane '%s'", job.name, job.lane_name)

    # -- public API for CLI / tools --

    def drain_output(self) -> list[str]:
        with self._queue_lock:
            items = list(self._output_queue)
            self._output_queue.clear()
            return items

    def status(self) -> dict[str, Any]:
        lane_stats = self.command_queue.stats()
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "job_count": len(self._jobs),
            "jobs": [j.name for j in self._jobs],
            "lanes": lane_stats,
            "tick_interval": f"{TICK_INTERVAL}s",
        }

    def get_heartbeat(self) -> HeartbeatJob | None:
        for job in self._jobs:
            if isinstance(job, HeartbeatJob):
                return job
        return None

    def get_cron_service(self) -> CronService | None:
        for job in self._jobs:
            if isinstance(job, CronService):
                return job
        return None

    def heartbeat_status(self) -> dict[str, Any]:
        hb = self.get_heartbeat()
        if hb is None:
            return {"enabled": False, "reason": "no heartbeat job registered"}
        return hb.status()

    def trigger_heartbeat(self) -> str:
        hb = self.get_heartbeat()
        if hb is None:
            return "No heartbeat job registered"
        return hb.trigger()

    def list_cron_jobs(self) -> list[dict[str, Any]]:
        cs = self.get_cron_service()
        if cs is None:
            return []
        return cs.list_jobs()

    def trigger_cron_job(self, job_id: str) -> str:
        cs = self.get_cron_service()
        if cs is None:
            return "No cron service registered"
        return cs.trigger_job(job_id)
