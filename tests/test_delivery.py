"""Tests for reliable delivery: send_with_retry and cron error wiring."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pip_agent.channels import Channel, CLIChannel, send_with_retry, BACKOFF_SCHEDULE
from pip_agent.scheduler import CronService, CronJob, CronJobSource, CRON_AUTO_DISABLE_THRESHOLD


# ---------------------------------------------------------------------------
# send_with_retry
# ---------------------------------------------------------------------------

class _MockChannel(Channel):
    """Channel that fails a configurable number of times then succeeds."""

    name = "test"

    def __init__(self, fail_count: int = 0):
        self._fail_count = fail_count
        self._attempts = 0

    def send(self, to: str, text: str, **kw) -> bool:
        self._attempts += 1
        if self._attempts <= self._fail_count:
            return False
        return True


class TestSendWithRetry:
    def test_success_on_first_try(self):
        ch = _MockChannel(fail_count=0)
        assert send_with_retry(ch, "user", "hello") is True
        assert ch._attempts == 1

    @patch("pip_agent.channels.time.sleep")
    def test_retries_on_failure(self, mock_sleep):
        ch = _MockChannel(fail_count=2)
        assert send_with_retry(ch, "user", "hello") is True
        assert ch._attempts == 3
        assert mock_sleep.call_count == 2

    @patch("pip_agent.channels.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep):
        ch = _MockChannel(fail_count=100)
        assert send_with_retry(ch, "user", "hello") is False
        assert ch._attempts == 1 + len(BACKOFF_SCHEDULE)

    def test_cli_skips_chunking_and_retry(self, capsys):
        ch = CLIChannel()
        assert send_with_retry(ch, "cli-user", "hello") is True
        captured = capsys.readouterr()
        assert "hello" in captured.out

    @patch("pip_agent.channels.time.sleep")
    def test_chunks_long_text(self, mock_sleep):
        ch = _MockChannel(fail_count=0)
        ch.name = "wechat"  # limit=2000, so 3000 chars will be split
        text = "A" * 3000
        assert send_with_retry(ch, "user", text) is True
        assert ch._attempts >= 2

    @patch("pip_agent.channels.time.sleep")
    def test_partial_failure(self, mock_sleep):
        """If one chunk fails permanently, returns False but still sends others."""
        call_count = 0

        class _PartialChannel(Channel):
            name = "test"

            def send(self, to: str, text: str, **kw) -> bool:
                nonlocal call_count
                call_count += 1
                if "FAIL" in text:
                    return False
                return True

        ch = _PartialChannel()
        text = "OK chunk\n\n" + "FAIL" * 500
        result = send_with_retry(ch, "user", text)
        assert result is False


# ---------------------------------------------------------------------------
# CronService.report_outcome
# ---------------------------------------------------------------------------

class TestCronReportOutcome:
    @pytest.fixture
    def cron_svc(self, tmp_path: Path):
        cron_file = tmp_path / "CRON.json"
        cron_file.write_text(json.dumps({
            "jobs": [
                {
                    "id": "test-job",
                    "name": "Test Job",
                    "enabled": True,
                    "schedule": {"kind": "every", "every_seconds": 3600},
                    "payload": {"kind": "agent_turn", "message": "do stuff"},
                    "source": {"channel": "cli", "peer_id": "cli-user", "sender_id": ""},
                    "delete_after_run": False,
                    "consecutive_errors": 0,
                },
            ],
        }), encoding="utf-8")
        return CronService(cron_file)

    def test_success_resets_errors(self, cron_svc: CronService):
        job = cron_svc.jobs[0]
        job.consecutive_errors = 3
        cron_svc.report_outcome("test-job", success=True)
        assert job.consecutive_errors == 0

    def test_failure_increments_errors(self, cron_svc: CronService):
        cron_svc.report_outcome("test-job", success=False)
        assert cron_svc.jobs[0].consecutive_errors == 1

    def test_auto_disable_on_threshold(self, cron_svc: CronService):
        for _ in range(CRON_AUTO_DISABLE_THRESHOLD):
            cron_svc.report_outcome("test-job", success=False)
        job = cron_svc.jobs[0]
        assert job.enabled is False
        assert job.consecutive_errors == CRON_AUTO_DISABLE_THRESHOLD

    def test_persists_to_disk(self, cron_svc: CronService):
        cron_svc.report_outcome("test-job", success=False)
        data = json.loads(cron_svc.cron_file.read_text(encoding="utf-8"))
        assert data["jobs"][0]["consecutive_errors"] == 1

    def test_unknown_job_is_noop(self, cron_svc: CronService):
        cron_svc.report_outcome("nonexistent", success=False)
        assert cron_svc.jobs[0].consecutive_errors == 0
