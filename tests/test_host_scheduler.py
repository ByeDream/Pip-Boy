"""Unit tests for the pure-logic parts of ``host_scheduler`` + job CRUD.

The background thread is not exercised here (that lands in Phase 11's
integration harness). These tests focus on determinism: schedule maths,
active-window calculation, cron.json persistence, and enqueue side effects.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pip_agent.channels import InboundMessage
from pip_agent.host_scheduler import (
    _CRON_AUTO_DISABLE_THRESHOLD,
    _DREAM_CYCLE_KEY,
    _DREAM_LAST_AT_KEY,
    HostScheduler,
    _HeartbeatState,
    _in_active_window,
    _in_dream_window,
    _next_cron_fire,
    _next_fire_at,
    _Sender,
    _validate_schedule,
)


def _fake_registry(pip_dir: Path, *, agent_id: str = "pip-boy"):
    """Minimal :class:`AgentRegistry`-shaped stub for scheduler tests.

    Exposes a single agent whose ``pip_dir`` is ``pip_dir`` (anything
    matching the :class:`AgentPaths` surface the scheduler reads).
    """
    paths = SimpleNamespace(
        pip_dir=pip_dir,
        workspace_pip_dir=pip_dir.parent,
        cwd=pip_dir.parent,
    )
    cfg = SimpleNamespace(id=agent_id)
    return SimpleNamespace(
        list_agents=lambda: [cfg],
        paths_for=lambda aid: paths if aid == agent_id else None,
    )

# ---------------------------------------------------------------------------
# Schedule maths
# ---------------------------------------------------------------------------


class TestNextFireAt:
    def test_at_future(self):
        now = 1_000_000.0
        assert _next_fire_at("at", {"timestamp": now + 500}, now=now) == now + 500

    def test_at_past_returns_none(self):
        now = 1_000_000.0
        assert _next_fire_at("at", {"timestamp": now - 500}, now=now) is None

    def test_every_adds_seconds(self):
        now = 1_000_000.0
        assert _next_fire_at("every", {"seconds": 60}, now=now) == now + 60

    def test_every_non_positive_is_none(self):
        assert _next_fire_at("every", {"seconds": 0}, now=0) is None
        assert _next_fire_at("every", {"seconds": -5}, now=0) is None

    def test_unknown_kind(self):
        assert _next_fire_at("weekly", {}, now=0) is None


class TestNextCronFire:
    def test_daily_future_same_day(self):
        now_dt = datetime(2026, 4, 20, 8, 0, 0)
        now = now_dt.timestamp()
        fire = _next_cron_fire("30 9 * * *", now=now)
        assert fire is not None
        assert datetime.fromtimestamp(fire) == datetime(2026, 4, 20, 9, 30, 0)

    def test_daily_past_rolls_to_tomorrow(self):
        now = datetime(2026, 4, 20, 10, 0, 0).timestamp()
        fire = _next_cron_fire("30 9 * * *", now=now)
        assert fire is not None
        assert datetime.fromtimestamp(fire) == datetime(2026, 4, 21, 9, 30, 0)

    def test_hourly_advances_one_hour_when_past(self):
        now = datetime(2026, 4, 20, 10, 31, 0).timestamp()
        fire = _next_cron_fire("30 * * * *", now=now)
        assert fire is not None
        assert datetime.fromtimestamp(fire) == datetime(2026, 4, 20, 11, 30, 0)

    def test_hourly_in_current_hour_when_future(self):
        now = datetime(2026, 4, 20, 10, 15, 0).timestamp()
        fire = _next_cron_fire("30 * * * *", now=now)
        assert datetime.fromtimestamp(fire) == datetime(2026, 4, 20, 10, 30, 0)

    def test_rejects_dom(self):
        assert _next_cron_fire("0 9 1 * *", now=time.time()) is None

    def test_rejects_bad_grammar(self):
        assert _next_cron_fire("garbage", now=time.time()) is None
        assert _next_cron_fire("99 25 * * *", now=time.time()) is None


class TestValidateSchedule:
    def test_at_requires_timestamp(self):
        assert _validate_schedule("at", {}) is not None

    def test_every_requires_positive_seconds(self):
        assert _validate_schedule("every", {"seconds": 0}) is not None
        assert _validate_schedule("every", {"seconds": 60}) is None

    def test_cron_rejects_unsupported(self):
        assert _validate_schedule("cron", {"expr": "*/5 * * * *"}) is not None

    def test_cron_accepts_daily(self):
        assert _validate_schedule("cron", {"expr": "0 9 * * *"}) is None


# ---------------------------------------------------------------------------
# Active window
# ---------------------------------------------------------------------------


class TestActiveWindow:
    @patch("pip_agent.host_scheduler.settings")
    def test_daytime_window(self, mock_settings):
        mock_settings.heartbeat_active_start = 9
        mock_settings.heartbeat_active_end = 22
        noon = datetime(2026, 4, 20, 12, 0, 0).timestamp()
        early = datetime(2026, 4, 20, 6, 0, 0).timestamp()
        assert _in_active_window(noon) is True
        assert _in_active_window(early) is False

    @patch("pip_agent.host_scheduler.settings")
    def test_wrap_around_night(self, mock_settings):
        mock_settings.heartbeat_active_start = 22
        mock_settings.heartbeat_active_end = 6
        midnight = datetime(2026, 4, 20, 1, 0, 0).timestamp()
        afternoon = datetime(2026, 4, 20, 13, 0, 0).timestamp()
        assert _in_active_window(midnight) is True
        assert _in_active_window(afternoon) is False


# ---------------------------------------------------------------------------
# Scheduler job CRUD + enqueue
# ---------------------------------------------------------------------------


def _make_sched(tmp_path: Path) -> tuple[HostScheduler, list, threading.Lock]:
    queue: list = []
    lock = threading.Lock()
    stop = threading.Event()
    pip_dir = tmp_path / "agents" / "pip-boy"
    pip_dir.mkdir(parents=True)
    sched = HostScheduler(
        registry=_fake_registry(pip_dir),
        msg_queue=queue,
        q_lock=lock,
        stop_event=stop,
    )
    return sched, queue, lock


class TestSchedulerJobCrud:
    def test_add_every_persists_to_disk(self, tmp_path: Path):
        sched, _, _ = _make_sched(tmp_path)
        reply = sched.add_job(
            name="tick", schedule_kind="every", schedule_config={"seconds": 60},
            message="hello", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        assert "Scheduled 'tick'" in reply
        assert (tmp_path / "agents" / "pip-boy" / "cron.json").is_file()
        jobs = sched.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["message"] == "hello"

    def test_add_rejects_invalid_schedule(self, tmp_path: Path):
        sched, _, _ = _make_sched(tmp_path)
        reply = sched.add_job(
            name="bad", schedule_kind="every", schedule_config={"seconds": 0},
            message="x", channel="cli", peer_id="p",
            sender_id="s", agent_id="pip-boy",
        )
        assert reply.startswith("Error:")
        assert sched.list_jobs() == []

    def test_add_requires_agent_id(self, tmp_path: Path):
        sched, _, _ = _make_sched(tmp_path)
        reply = sched.add_job(
            name="n", schedule_kind="every", schedule_config={"seconds": 60},
            message="m", channel="cli", peer_id="p",
            sender_id="s", agent_id="",
        )
        assert "agent_id" in reply

    def test_remove_by_id(self, tmp_path: Path):
        sched, _, _ = _make_sched(tmp_path)
        sched.add_job(
            name="a", schedule_kind="every", schedule_config={"seconds": 60},
            message="m", channel="cli", peer_id="p",
            sender_id="s", agent_id="pip-boy",
        )
        jid = sched.list_jobs()[0]["id"]
        assert "Removed" in sched.remove_job(jid)
        assert sched.list_jobs() == []

    def test_update_changes_fields(self, tmp_path: Path):
        sched, _, _ = _make_sched(tmp_path)
        sched.add_job(
            name="a", schedule_kind="every", schedule_config={"seconds": 60},
            message="old", channel="cli", peer_id="p",
            sender_id="s", agent_id="pip-boy",
        )
        jid = sched.list_jobs()[0]["id"]
        assert "Updated" in sched.update_job(jid, message="new", enabled=False)
        job = sched.list_jobs()[0]
        assert job["message"] == "new"
        assert job["enabled"] is False

    def test_update_unknown_id(self, tmp_path: Path):
        sched, _, _ = _make_sched(tmp_path)
        assert "not found" in sched.update_job("nosuchid", message="x")


class TestSchedulerTickFiresJobs:
    def test_every_job_fires_and_reschedules(self, tmp_path: Path):
        sched, queue, _ = _make_sched(tmp_path)
        sched.add_job(
            name="beep", schedule_kind="every", schedule_config={"seconds": 60},
            message="ping", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        # Force the single job's next_fire_at into the past and tick.
        jobs = sched._load_jobs("pip-boy")
        jobs[0]["next_fire_at"] = time.time() - 1
        sched._save_jobs("pip-boy", jobs)

        sched._tick(time.time())

        assert len(queue) == 1
        msg = queue[0]
        assert isinstance(msg, InboundMessage)
        assert msg.sender_id == _Sender.CRON
        assert msg.text == "ping"
        assert msg.agent_id == "pip-boy"
        # Next fire was rescheduled into the future.
        assert sched._load_jobs("pip-boy")[0]["next_fire_at"] > time.time()

    def test_at_job_disables_after_firing(self, tmp_path: Path):
        sched, queue, _ = _make_sched(tmp_path)
        past = time.time() - 10
        sched.add_job(
            name="once", schedule_kind="at",
            schedule_config={"timestamp": time.time() + 100},  # first must pass validation
            message="go", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        # Then force fire_at into the past for this tick.
        jobs = sched._load_jobs("pip-boy")
        jobs[0]["next_fire_at"] = past
        sched._save_jobs("pip-boy", jobs)

        sched._tick(time.time())
        assert len(queue) == 1
        assert sched._load_jobs("pip-boy")[0]["enabled"] is False


class TestHeartbeatTick:
    @patch("pip_agent.host_scheduler.settings")
    def test_heartbeat_fires_when_md_present(self, mock_settings, tmp_path: Path):
        mock_settings.heartbeat_interval = 60
        mock_settings.heartbeat_active_start = 0
        mock_settings.heartbeat_active_end = 24
        sched, queue, _ = _make_sched(tmp_path)
        (tmp_path / "agents" / "pip-boy" / "HEARTBEAT.md").write_text(
            "check memory", encoding="utf-8",
        )
        # Pre-seed state so the first tick is past the interval — otherwise
        # the "don't fire on startup" guard (see ``_HeartbeatState`` docstring)
        # swallows tick #1.
        now = time.time()
        sched._heartbeat_state["pip-boy"] = _HeartbeatState(last_fire_at=now - 120)
        sched._tick(now)
        assert len(queue) == 1
        assert queue[0].sender_id == _Sender.HEARTBEAT
        assert queue[0].text == "check memory"

    @patch("pip_agent.host_scheduler.settings")
    def test_heartbeat_respects_interval(self, mock_settings, tmp_path: Path):
        mock_settings.heartbeat_interval = 60
        mock_settings.heartbeat_active_start = 0
        mock_settings.heartbeat_active_end = 24
        sched, queue, _ = _make_sched(tmp_path)
        (tmp_path / "agents" / "pip-boy" / "HEARTBEAT.md").write_text(
            "ping", encoding="utf-8",
        )
        now = time.time()
        sched._heartbeat_state["pip-boy"] = _HeartbeatState(last_fire_at=now - 120)
        sched._tick(now)
        sched._tick(now)  # same tick, no double-fire
        assert len(queue) == 1

    @patch("pip_agent.host_scheduler.settings")
    def test_heartbeat_skipped_outside_window(self, mock_settings, tmp_path: Path):
        mock_settings.heartbeat_interval = 60
        mock_settings.heartbeat_active_start = 9
        mock_settings.heartbeat_active_end = 22
        sched, queue, _ = _make_sched(tmp_path)
        (tmp_path / "agents" / "pip-boy" / "HEARTBEAT.md").write_text(
            "ping", encoding="utf-8",
        )
        early = datetime(2026, 4, 20, 3, 0, 0).timestamp()
        sched._tick(early)
        assert queue == []

    @patch("pip_agent.host_scheduler.settings")
    def test_heartbeat_does_not_fire_on_first_tick(
        self, mock_settings, tmp_path: Path
    ):
        """Regression: the first tick after startup must NOT fire the heartbeat.

        Cold-start SDK latency (subprocess spawn + JSONL transcript load) can
        be 30-90 s. If the heartbeat fires on tick #1 it front-runs the user's
        first message and burns that latency on a keepalive. ``_HeartbeatState``
        seeds ``last_fire_at`` with the current time to keep tick #1 quiet.
        """
        mock_settings.heartbeat_interval = 60
        mock_settings.heartbeat_active_start = 0
        mock_settings.heartbeat_active_end = 24
        sched, queue, _ = _make_sched(tmp_path)
        (tmp_path / "agents" / "pip-boy" / "HEARTBEAT.md").write_text(
            "ping", encoding="utf-8",
        )

        sched._tick(time.time())

        assert queue == []
        # State must exist so tick #2 computes the interval from real elapsed.
        assert "pip-boy" in sched._heartbeat_state

    @patch("pip_agent.host_scheduler.settings")
    def test_heartbeat_fires_after_interval_on_second_tick(
        self, mock_settings, tmp_path: Path
    ):
        """Tick #2, once the interval has actually elapsed, must fire."""
        mock_settings.heartbeat_interval = 60
        mock_settings.heartbeat_active_start = 0
        mock_settings.heartbeat_active_end = 24
        sched, queue, _ = _make_sched(tmp_path)
        (tmp_path / "agents" / "pip-boy" / "HEARTBEAT.md").write_text(
            "ping", encoding="utf-8",
        )
        t0 = time.time()
        sched._tick(t0)  # seeds state, does not fire
        sched._tick(t0 + 61)  # interval elapsed — should fire

        assert len(queue) == 1
        assert queue[0].sender_id == _Sender.HEARTBEAT


class TestCronCoalescing:
    """Regression tests for the "every-30s job floods the queue" bug.

    Without coalescing, a cron with ``every: 30s`` whose turn takes 40s
    stacks one new payload per tick and buries any user message under N
    copies of the same cron. The scheduler must refuse to enqueue while a
    previous copy of the same job is either still in the queue or still
    executing inside ``AgentHost.process_inbound``; :meth:`HostScheduler.ack`
    is the handshake that releases the next fire.
    """

    def test_cron_message_carries_source_job_id(self, tmp_path: Path):
        sched, queue, _ = _make_sched(tmp_path)
        sched.add_job(
            name="beep", schedule_kind="every", schedule_config={"seconds": 60},
            message="ping", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        jobs = sched._load_jobs("pip-boy")
        jid = jobs[0]["id"]
        jobs[0]["next_fire_at"] = time.time() - 1
        sched._save_jobs("pip-boy", jobs)

        sched._tick(time.time())

        assert len(queue) == 1
        assert queue[0].source_job_id == f"cron:{jid}"

    def test_second_tick_coalesces_while_first_unacked(self, tmp_path: Path):
        """No ack between fires → second fire is suppressed, queue stays at 1."""
        sched, queue, _ = _make_sched(tmp_path)
        sched.add_job(
            name="beep", schedule_kind="every", schedule_config={"seconds": 1},
            message="ping", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        # Fire once.
        jobs = sched._load_jobs("pip-boy")
        jobs[0]["next_fire_at"] = time.time() - 1
        sched._save_jobs("pip-boy", jobs)
        sched._tick(time.time())
        assert len(queue) == 1

        # Simulate 2 seconds passing. The scheduler tick would normally
        # enqueue again — but the host hasn't ack'd the first message yet.
        jobs = sched._load_jobs("pip-boy")
        jobs[0]["next_fire_at"] = time.time() - 1
        sched._save_jobs("pip-boy", jobs)
        sched._tick(time.time())

        assert len(queue) == 1, "second tick must coalesce while first is pending"

    def test_track_exit_releases_coalesce(self, tmp_path: Path):
        """After ``with scheduler.track(inbound)`` exits, the next tick fires."""
        sched, queue, _ = _make_sched(tmp_path)
        sched.add_job(
            name="beep", schedule_kind="every", schedule_config={"seconds": 1},
            message="ping", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        jobs = sched._load_jobs("pip-boy")
        jid = jobs[0]["id"]
        jobs[0]["next_fire_at"] = time.time() - 1
        sched._save_jobs("pip-boy", jobs)
        sched._tick(time.time())
        assert len(queue) == 1

        # Host "processed" it — release via the context manager.
        inbound = queue.pop(0)
        with sched.track(inbound):
            pass

        jobs = sched._load_jobs("pip-boy")
        jobs[0]["next_fire_at"] = time.time() - 1
        sched._save_jobs("pip-boy", jobs)
        sched._tick(time.time())

        assert len(queue) == 1
        assert queue[0].source_job_id == f"cron:{jid}"

    def test_two_different_cron_jobs_do_not_coalesce(self, tmp_path: Path):
        """Coalesce key is per-job — two distinct jobs fire independently."""
        sched, queue, _ = _make_sched(tmp_path)
        sched.add_job(
            name="a", schedule_kind="every", schedule_config={"seconds": 60},
            message="A", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        sched.add_job(
            name="b", schedule_kind="every", schedule_config={"seconds": 60},
            message="B", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        jobs = sched._load_jobs("pip-boy")
        for j in jobs:
            j["next_fire_at"] = time.time() - 1
        sched._save_jobs("pip-boy", jobs)

        sched._tick(time.time())

        assert len(queue) == 2
        texts = sorted(m.text for m in queue)
        assert texts == ["A", "B"]

    def test_track_with_empty_key_is_noop(self, tmp_path: Path):
        """User messages have source_job_id="" — track must accept that."""
        sched, _, _ = _make_sched(tmp_path)
        inbound = InboundMessage(text="hi", sender_id="cli-user")
        with sched.track(inbound):
            pass  # must not raise and must not touch pending set


class TestHeartbeatCoalescing:
    @patch("pip_agent.host_scheduler.settings")
    def test_heartbeat_carries_source_job_id(self, mock_settings, tmp_path: Path):
        mock_settings.heartbeat_interval = 60
        mock_settings.heartbeat_active_start = 0
        mock_settings.heartbeat_active_end = 24
        sched, queue, _ = _make_sched(tmp_path)
        (tmp_path / "agents" / "pip-boy" / "HEARTBEAT.md").write_text(
            "ping", encoding="utf-8",
        )
        now = time.time()
        sched._heartbeat_state["pip-boy"] = _HeartbeatState(last_fire_at=now - 120)
        sched._tick(now)

        assert len(queue) == 1
        assert queue[0].source_job_id == "hb:pip-boy"

    @patch("pip_agent.host_scheduler.settings")
    def test_heartbeat_coalesces_while_unacked(
        self, mock_settings, tmp_path: Path,
    ):
        mock_settings.heartbeat_interval = 60
        mock_settings.heartbeat_active_start = 0
        mock_settings.heartbeat_active_end = 24
        sched, queue, _ = _make_sched(tmp_path)
        (tmp_path / "agents" / "pip-boy" / "HEARTBEAT.md").write_text(
            "ping", encoding="utf-8",
        )
        t0 = time.time()
        sched._heartbeat_state["pip-boy"] = _HeartbeatState(last_fire_at=t0 - 120)
        sched._tick(t0)
        assert len(queue) == 1

        # Another full interval passes. Without ack, second tick must NOT
        # stack another copy.
        sched._tick(t0 + 61)

        assert len(queue) == 1, "heartbeat must coalesce while previous unacked"

    @patch("pip_agent.host_scheduler.settings")
    def test_heartbeat_fires_again_after_track_exit(
        self, mock_settings, tmp_path: Path,
    ):
        mock_settings.heartbeat_interval = 60
        mock_settings.heartbeat_active_start = 0
        mock_settings.heartbeat_active_end = 24
        sched, queue, _ = _make_sched(tmp_path)
        (tmp_path / "agents" / "pip-boy" / "HEARTBEAT.md").write_text(
            "ping", encoding="utf-8",
        )
        t0 = time.time()
        sched._heartbeat_state["pip-boy"] = _HeartbeatState(last_fire_at=t0 - 120)
        sched._tick(t0)
        assert len(queue) == 1

        with sched.track(queue.pop(0)):
            pass

        sched._tick(t0 + 61)

        assert len(queue) == 1
        assert queue[0].source_job_id == "hb:pip-boy"


class TestInboundSortKey:
    """User messages must drain before cron / heartbeat in the same batch."""

    def test_user_beats_cron_and_heartbeat(self):
        from pip_agent.agent_host import _inbound_sort_key
        from pip_agent.channels import InboundMessage

        hb = InboundMessage(text="ping", sender_id=_Sender.HEARTBEAT)
        cron = InboundMessage(text="beep", sender_id=_Sender.CRON)
        user = InboundMessage(text="hi", sender_id="cli-user")

        batch = [hb, cron, user]
        batch.sort(key=_inbound_sort_key)

        assert batch[0] is user
        assert batch[1] is cron
        assert batch[2] is hb


class TestCronAutoDisable:
    """A cron job that keeps failing must eventually turn itself off."""

    def _make_failing_cron(self, tmp_path: Path) -> tuple[HostScheduler, list, str]:
        sched, queue, _ = _make_sched(tmp_path)
        sched.add_job(
            name="flaky", schedule_kind="every", schedule_config={"seconds": 60},
            message="does not matter", channel="cli", peer_id="cli-user",
            sender_id="owner", agent_id="pip-boy",
        )
        jid = sched._load_jobs("pip-boy")[0]["id"]
        return sched, queue, jid

    def test_success_resets_counter(self, tmp_path: Path):
        sched, _, jid = self._make_failing_cron(tmp_path)
        jobs = sched._load_jobs("pip-boy")
        jobs[0]["consecutive_errors"] = 3
        sched._save_jobs("pip-boy", jobs)

        inbound = InboundMessage(
            text="x", sender_id=_Sender.CRON, agent_id="pip-boy",
            source_job_id=f"cron:{jid}",
        )
        with sched.track(inbound):
            pass  # success

        assert sched._load_jobs("pip-boy")[0]["consecutive_errors"] == 0

    def test_failure_increments_counter(self, tmp_path: Path):
        sched, _, jid = self._make_failing_cron(tmp_path)

        inbound = InboundMessage(
            text="x", sender_id=_Sender.CRON, agent_id="pip-boy",
            source_job_id=f"cron:{jid}",
        )
        with sched.track(inbound) as t:
            t.failure("boom")

        job = sched._load_jobs("pip-boy")[0]
        assert job["consecutive_errors"] == 1
        assert job["enabled"] is True  # still below threshold

    def test_exception_counts_as_failure(self, tmp_path: Path):
        sched, _, jid = self._make_failing_cron(tmp_path)

        inbound = InboundMessage(
            text="x", sender_id=_Sender.CRON, agent_id="pip-boy",
            source_job_id=f"cron:{jid}",
        )
        try:
            with sched.track(inbound):
                raise RuntimeError("SDK exploded")
        except RuntimeError:
            pass  # re-raise is intentional — host's own error handling still runs

        assert sched._load_jobs("pip-boy")[0]["consecutive_errors"] == 1

    def test_auto_disable_at_threshold(self, tmp_path: Path):
        sched, _, jid = self._make_failing_cron(tmp_path)

        inbound = InboundMessage(
            text="x", sender_id=_Sender.CRON, agent_id="pip-boy",
            source_job_id=f"cron:{jid}",
        )
        for _ in range(_CRON_AUTO_DISABLE_THRESHOLD):
            with sched.track(inbound) as t:
                t.failure("nope")

        job = sched._load_jobs("pip-boy")[0]
        assert job["consecutive_errors"] == _CRON_AUTO_DISABLE_THRESHOLD
        assert job["enabled"] is False

    def test_disabled_job_stops_firing(self, tmp_path: Path):
        """Once auto-disabled, the scheduler must stop enqueuing it."""
        sched, queue, jid = self._make_failing_cron(tmp_path)

        inbound = InboundMessage(
            text="x", sender_id=_Sender.CRON, agent_id="pip-boy",
            source_job_id=f"cron:{jid}",
        )
        for _ in range(_CRON_AUTO_DISABLE_THRESHOLD):
            with sched.track(inbound) as t:
                t.failure("nope")

        # Force the next_fire_at well into the past, then tick.
        jobs = sched._load_jobs("pip-boy")
        jobs[0]["next_fire_at"] = time.time() - 1
        sched._save_jobs("pip-boy", jobs)
        sched._tick(time.time())

        assert queue == [], "disabled cron must not enqueue"

    def test_heartbeat_does_not_update_cron_counters(self, tmp_path: Path):
        """Heartbeat track() must not mutate any cron.json."""
        sched, _, jid = self._make_failing_cron(tmp_path)
        before = sched._load_jobs("pip-boy")

        hb = InboundMessage(
            text="ping", sender_id=_Sender.HEARTBEAT, agent_id="pip-boy",
            source_job_id="hb:pip-boy",
        )
        with sched.track(hb) as t:
            t.failure("irrelevant")

        after = sched._load_jobs("pip-boy")
        assert before == after, "heartbeat failures must not touch cron state"


# ---------------------------------------------------------------------------
# Dream (L2 consolidate + L3 axiom distillation) gating
# ---------------------------------------------------------------------------


def _epoch_at_hour(hour: int) -> float:
    """Return a local epoch whose ``datetime.fromtimestamp`` hour == ``hour``."""
    today = datetime.now().replace(
        hour=hour, minute=0, second=0, microsecond=0,
    )
    return today.timestamp()


class TestInDreamWindow:
    def test_simple_window(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "dream_hour_start", 2)
        monkeypatch.setattr(config.settings, "dream_hour_end", 5)

        assert _in_dream_window(_epoch_at_hour(2)) is True
        assert _in_dream_window(_epoch_at_hour(4)) is True
        assert _in_dream_window(_epoch_at_hour(5)) is False  # half-open
        assert _in_dream_window(_epoch_at_hour(1)) is False

    def test_wrap_around_window(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "dream_hour_start", 22)
        monkeypatch.setattr(config.settings, "dream_hour_end", 5)

        assert _in_dream_window(_epoch_at_hour(22)) is True
        assert _in_dream_window(_epoch_at_hour(23)) is True
        assert _in_dream_window(_epoch_at_hour(3)) is True
        assert _in_dream_window(_epoch_at_hour(6)) is False
        assert _in_dream_window(_epoch_at_hour(12)) is False

    def test_start_equals_end_disables(self, monkeypatch):
        """Explicit ``start == end`` means "Dream off", not "always on"."""
        from pip_agent import config

        monkeypatch.setattr(config.settings, "dream_hour_start", 3)
        monkeypatch.setattr(config.settings, "dream_hour_end", 3)

        # Any hour at all — must be off.
        for h in range(24):
            assert _in_dream_window(_epoch_at_hour(h)) is False


class TestTickDreamGating:
    """All five gates from ``_tick_dream`` docstring, tested independently."""

    def _sched(self, tmp_path: Path) -> tuple[HostScheduler, Path]:
        pip_dir = tmp_path / ".pip" / "agents" / "pip-boy"
        pip_dir.mkdir(parents=True)
        queue: list[InboundMessage] = []
        sched = HostScheduler(
            registry=_fake_registry(pip_dir),
            msg_queue=queue,
            q_lock=threading.Lock(),
            stop_event=threading.Event(),
        )
        return sched, pip_dir

    def _spy_spawn(self, sched: HostScheduler, monkeypatch) -> list[tuple]:
        """Replace the worker-thread spawn with a recorder so we can
        assert gate behaviour without actually running consolidate."""
        calls: list[tuple] = []

        class _FakeThread:
            def __init__(self, *a, target=None, args=(), **kw):
                calls.append(args)
                # Mimic the real worker discarding the running guard so
                # successive test ticks don't wedge the test harness.
                aid = args[0]
                with sched._dream_lock:
                    sched._dream_running.discard(aid)

            def start(self):
                pass

        monkeypatch.setattr(
            "pip_agent.host_scheduler.threading.Thread", _FakeThread,
        )
        return calls

    def test_skip_outside_window(self, tmp_path: Path, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "dream_hour_start", 2)
        monkeypatch.setattr(config.settings, "dream_hour_end", 5)
        monkeypatch.setattr(config.settings, "dream_min_observations", 0)
        monkeypatch.setattr(config.settings, "dream_inactive_minutes", 0)

        sched, agent_dir = self._sched(tmp_path)
        calls = self._spy_spawn(sched, monkeypatch)

        sched._tick_dream("pip-boy", _epoch_at_hour(12))

        assert calls == []

    def test_skip_when_already_running(
        self, tmp_path: Path, monkeypatch,
    ):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "dream_hour_start", 2)
        monkeypatch.setattr(config.settings, "dream_hour_end", 5)
        monkeypatch.setattr(config.settings, "dream_min_observations", 0)
        monkeypatch.setattr(config.settings, "dream_inactive_minutes", 0)

        sched, agent_dir = self._sched(tmp_path)
        calls = self._spy_spawn(sched, monkeypatch)

        # Simulate an in-flight worker for this agent.
        sched._dream_running.add("pip-boy")
        sched._tick_dream("pip-boy", _epoch_at_hour(3))

        assert calls == []

    def test_skip_when_last_dream_within_window(
        self, tmp_path: Path, monkeypatch,
    ):
        from pip_agent import config
        from pip_agent.memory import MemoryStore

        monkeypatch.setattr(config.settings, "dream_hour_start", 2)
        monkeypatch.setattr(config.settings, "dream_hour_end", 5)
        monkeypatch.setattr(config.settings, "dream_min_observations", 0)
        monkeypatch.setattr(config.settings, "dream_inactive_minutes", 0)

        sched, agent_dir = self._sched(tmp_path)
        calls = self._spy_spawn(sched, monkeypatch)

        # Stamp a Dream that happened inside the current 3-hour window.
        now = _epoch_at_hour(4)
        store = MemoryStore(
            agent_dir=agent_dir,
            workspace_pip_dir=agent_dir.parent,
            agent_id="pip-boy",
        )
        store.save_state({_DREAM_LAST_AT_KEY: now - 600})  # 10 min ago

        sched._tick_dream("pip-boy", now)

        assert calls == []

    def test_skip_when_below_min_observations(
        self, tmp_path: Path, monkeypatch,
    ):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "dream_hour_start", 2)
        monkeypatch.setattr(config.settings, "dream_hour_end", 5)
        monkeypatch.setattr(config.settings, "dream_min_observations", 10)
        monkeypatch.setattr(config.settings, "dream_inactive_minutes", 0)

        sched, agent_dir = self._sched(tmp_path)
        calls = self._spy_spawn(sched, monkeypatch)

        # Zero observations on disk — below the threshold.
        sched._tick_dream("pip-boy", _epoch_at_hour(3))

        assert calls == []

    def test_skip_when_recent_activity(self, tmp_path: Path, monkeypatch):
        from pip_agent import config
        from pip_agent.memory import MemoryStore

        monkeypatch.setattr(config.settings, "dream_hour_start", 2)
        monkeypatch.setattr(config.settings, "dream_hour_end", 5)
        monkeypatch.setattr(config.settings, "dream_min_observations", 0)
        monkeypatch.setattr(config.settings, "dream_inactive_minutes", 30)

        sched, agent_dir = self._sched(tmp_path)
        calls = self._spy_spawn(sched, monkeypatch)

        now = _epoch_at_hour(3)
        store = MemoryStore(
            agent_dir=agent_dir,
            workspace_pip_dir=agent_dir.parent,
            agent_id="pip-boy",
        )
        store.save_state({"last_activity_at": now - 60})  # 1 min ago

        sched._tick_dream("pip-boy", now)

        assert calls == []

    def test_spawns_worker_when_all_gates_pass(
        self, tmp_path: Path, monkeypatch,
    ):
        from pip_agent import config
        from pip_agent.memory import MemoryStore

        monkeypatch.setattr(config.settings, "dream_hour_start", 2)
        monkeypatch.setattr(config.settings, "dream_hour_end", 5)
        monkeypatch.setattr(config.settings, "dream_min_observations", 0)
        monkeypatch.setattr(config.settings, "dream_inactive_minutes", 0)

        sched, agent_dir = self._sched(tmp_path)
        calls = self._spy_spawn(sched, monkeypatch)

        # Past activity + no recent dream → all gates pass.
        now = _epoch_at_hour(3)
        store = MemoryStore(
            agent_dir=agent_dir,
            workspace_pip_dir=agent_dir.parent,
            agent_id="pip-boy",
        )
        store.save_state({"last_activity_at": now - 86400})  # 1 day ago

        sched._tick_dream("pip-boy", now)

        assert len(calls) == 1
        # args = (agent_id, started_at, observations)
        assert calls[0][0] == "pip-boy"


class TestRunDream:
    """Direct worker-body tests. These actually invoke the Dream pipeline
    (with mocked LLM + consolidate) to verify the state-write contract."""

    def _sched_and_store(self, tmp_path: Path):
        from pip_agent.memory import MemoryStore

        pip_dir = tmp_path / ".pip" / "agents" / "pip-boy"
        pip_dir.mkdir(parents=True)
        sched = HostScheduler(
            registry=_fake_registry(pip_dir),
            msg_queue=[],
            q_lock=threading.Lock(),
            stop_event=threading.Event(),
        )
        sched._dream_running.add("pip-boy")  # as the ticker would have set
        store = MemoryStore(
            agent_dir=pip_dir,
            workspace_pip_dir=pip_dir.parent,
            agent_id="pip-boy",
        )
        return sched, store

    def test_no_client_exits_cleanly(self, tmp_path: Path):
        sched, store = self._sched_and_store(tmp_path)

        with patch(
            "pip_agent.anthropic_client.build_anthropic_client",
            return_value=None,
        ):
            sched._run_dream("pip-boy", time.time(), [])

        # Guard must be released even on early exit.
        assert "pip-boy" not in sched._dream_running
        # State must NOT be stamped (we didn't actually do work).
        assert _DREAM_LAST_AT_KEY not in store.load_state()

    def test_happy_path_stamps_state_and_bumps_cycle(self, tmp_path: Path):
        sched, store = self._sched_and_store(tmp_path)

        fake_memories = [
            {"id": "m1", "text": "likes concise output", "count": 3,
             "stability": 0.8},
        ]

        with (
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=object(),
            ),
            patch(
                "pip_agent.memory.consolidate.consolidate",
                return_value=fake_memories,
            ),
            patch(
                "pip_agent.memory.consolidate.distill_axioms",
                return_value="- Be concise.",
            ),
        ):
            sched._run_dream("pip-boy", time.time(), [{"text": "foo"}])

        assert "pip-boy" not in sched._dream_running
        state = store.load_state()
        assert state[_DREAM_CYCLE_KEY] == 1
        assert state[_DREAM_LAST_AT_KEY] > 0
        assert store.load_memories() == fake_memories

    def test_exception_still_clears_guard(self, tmp_path: Path):
        sched, _ = self._sched_and_store(tmp_path)

        with (
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=object(),
            ),
            patch(
                "pip_agent.memory.consolidate.consolidate",
                side_effect=RuntimeError("boom"),
            ),
        ):
            # Must not propagate; must release guard.
            sched._run_dream("pip-boy", time.time(), [])

        assert "pip-boy" not in sched._dream_running

    def test_happy_path_purges_consumed_observations(self, tmp_path: Path):
        """Plan H5: after a successful Dream, the observations that
        fed it must be hard-deleted so the next Dream cycle does not
        re-consolidate the same signals (which would over-weight them
        in the L2 store and silently drift the memory model).

        We seed the store with three observations, two of them with
        timestamps ≤ ``started_at`` (the ones the Dream would have
        read) and one with a timestamp *after* ``started_at`` (as if
        it were written while Dream was running). Only the first two
        must disappear; the concurrent-write one must survive.
        """
        sched, store = self._sched_and_store(tmp_path)

        started_at = time.time()
        before_1 = {"ts": started_at - 30, "text": "old-1", "category": "lesson"}
        before_2 = {"ts": started_at - 10, "text": "old-2", "category": "lesson"}
        # Race window: written while the LLM call was running. Its ``ts``
        # is greater than ``started_at``, so the purge must leave it
        # alone — otherwise we lose the observation entirely.
        after_1 = {"ts": started_at + 5, "text": "new-1", "category": "lesson"}
        store.write_observations([before_1, before_2, after_1])

        with (
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=object(),
            ),
            patch(
                "pip_agent.memory.consolidate.consolidate",
                return_value=[{
                    "id": "m1", "text": "stay concise", "count": 2,
                    "stability": 0.7,
                }],
            ),
            patch(
                "pip_agent.memory.consolidate.distill_axioms",
                return_value="- be concise",
            ),
        ):
            sched._run_dream("pip-boy", started_at, [before_1, before_2])

        remaining = store.load_all_observations()
        remaining_texts = [o["text"] for o in remaining]
        assert remaining_texts == ["new-1"], remaining_texts
