"""Capability-ladder regression: TTY -> driver -> encoding, with logging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pip_agent.tui import capability
from pip_agent.tui.capability import (
    STAGE_DRIVER,
    STAGE_ENCODING,
    STAGE_READY,
    STAGE_TTY,
    STAGE_USER_OPTOUT,
    CapabilityResult,
    detect_tui_capability,
    write_capability_log,
)


# ---------------------------------------------------------------------------
# Per-stage probes
# ---------------------------------------------------------------------------


class TestUserOptOut:
    def test_force_no_tui_short_circuits(self) -> None:
        result = detect_tui_capability(force_no_tui=True)
        assert result.ok is False
        assert result.stage == STAGE_USER_OPTOUT
        # Stage 2/3/4 must NOT have been probed.
        assert [c[0] for c in result.checks] == [STAGE_USER_OPTOUT]


class TestLadderShortCircuits:
    """The ladder must stop at the first failing stage."""

    def test_tty_failure_skips_remaining(self) -> None:
        with patch.object(
            capability, "_probe_tty",
            return_value=(False, "stdin is not a TTY"),
        ), patch.object(
            capability, "_probe_driver",
            side_effect=AssertionError("must not run"),
        ), patch.object(
            capability, "_probe_encoding",
            side_effect=AssertionError("must not run"),
        ), patch.object(
            capability, "_PROBES",
            [
                (STAGE_TTY, capability._probe_tty),
                (STAGE_DRIVER, capability._probe_driver),
                (STAGE_ENCODING, capability._probe_encoding),
            ],
        ):
            result = detect_tui_capability()
        assert result.ok is False
        assert result.stage == STAGE_TTY
        assert [c[0] for c in result.checks] == [STAGE_TTY]

    def test_driver_failure_runs_only_tty_and_driver(self) -> None:
        with patch.object(
            capability, "_probe_tty", return_value=(True, "ok")
        ), patch.object(
            capability, "_probe_driver",
            return_value=(False, "win32 driver failed"),
        ), patch.object(
            capability, "_probe_encoding",
            side_effect=AssertionError("must not run"),
        ), patch.object(
            capability, "_PROBES",
            [
                (STAGE_TTY, capability._probe_tty),
                (STAGE_DRIVER, capability._probe_driver),
                (STAGE_ENCODING, capability._probe_encoding),
            ],
        ):
            result = detect_tui_capability()
        assert result.ok is False
        assert result.stage == STAGE_DRIVER
        assert [c[0] for c in result.checks] == [STAGE_TTY, STAGE_DRIVER]

    def test_encoding_failure_lists_three_checks(self) -> None:
        with patch.object(
            capability, "_probe_tty", return_value=(True, "ok")
        ), patch.object(
            capability, "_probe_driver", return_value=(True, "ok")
        ), patch.object(
            capability, "_probe_encoding",
            return_value=(False, "stdout encoding=cp936 not utf-8"),
        ), patch.object(
            capability, "_PROBES",
            [
                (STAGE_TTY, capability._probe_tty),
                (STAGE_DRIVER, capability._probe_driver),
                (STAGE_ENCODING, capability._probe_encoding),
            ],
        ):
            result = detect_tui_capability()
        assert result.ok is False
        assert result.stage == STAGE_ENCODING
        assert [c[0] for c in result.checks] == [
            STAGE_TTY, STAGE_DRIVER, STAGE_ENCODING,
        ]

    def test_all_pass_yields_ready(self) -> None:
        with patch.object(
            capability, "_probe_tty", return_value=(True, "tty ok")
        ), patch.object(
            capability, "_probe_driver", return_value=(True, "drv ok")
        ), patch.object(
            capability, "_probe_encoding", return_value=(True, "enc ok")
        ), patch.object(
            capability, "_PROBES",
            [
                (STAGE_TTY, capability._probe_tty),
                (STAGE_DRIVER, capability._probe_driver),
                (STAGE_ENCODING, capability._probe_encoding),
            ],
        ):
            result = detect_tui_capability()
        assert result.ok is True
        assert result.stage == STAGE_READY
        assert [c[0] for c in result.checks] == [
            STAGE_TTY, STAGE_DRIVER, STAGE_ENCODING, STAGE_READY,
        ]


class TestProbeDoesNotCrash:
    """A probe that raises must be caught — ladder is best-effort."""

    def test_probe_exception_treated_as_failure(self) -> None:
        def boom() -> tuple[bool, str]:
            raise RuntimeError("simulated oom")

        with patch.object(
            capability, "_PROBES", [(STAGE_TTY, boom)]
        ):
            result = detect_tui_capability()
        assert result.ok is False
        assert result.stage == STAGE_TTY
        assert "simulated oom" in result.detail


# ---------------------------------------------------------------------------
# Capability log writer
# ---------------------------------------------------------------------------


class TestWriteCapabilityLog:
    def test_writes_one_jsonl_line(self, tmp_path: Path) -> None:
        result = CapabilityResult(
            ok=False, stage=STAGE_TTY, detail="not a TTY",
            checks=[(STAGE_TTY, False, "not a TTY")],
        )
        write_capability_log(tmp_path, result)
        log_path = tmp_path / ".pip" / "tui_capability.log"
        assert log_path.exists()
        line = log_path.read_text(encoding="utf-8").strip()
        payload = json.loads(line)
        assert payload["ok"] is False
        assert payload["stage"] == STAGE_TTY
        assert payload["detail"] == "not a TTY"
        assert "ts" in payload

    def test_appends_across_runs(self, tmp_path: Path) -> None:
        for i in range(3):
            write_capability_log(
                tmp_path,
                CapabilityResult(
                    ok=False, stage=STAGE_TTY, detail=f"run-{i}",
                    checks=[(STAGE_TTY, False, f"run-{i}")],
                ),
            )
        log_path = tmp_path / ".pip" / "tui_capability.log"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        details = [json.loads(ln)["detail"] for ln in lines]
        assert details == ["run-0", "run-1", "run-2"]

    def test_unwritable_dir_logged_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A read-only filesystem must not crash boot."""
        target = tmp_path / "ro"
        target.mkdir()
        # Patch ``Path.mkdir`` so the writer's mkdir(.pip) raises;
        # OSError gets logged at WARNING, not propagated.
        with patch(
            "pathlib.Path.mkdir", side_effect=OSError("read-only fs")
        ), caplog.at_level("WARNING", logger=capability.log.name):
            write_capability_log(
                target,
                CapabilityResult(
                    ok=False, stage=STAGE_TTY, detail="x",
                    checks=[],
                ),
            )
        assert "Failed to write tui_capability.log" in caplog.text
