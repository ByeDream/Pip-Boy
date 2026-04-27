"""Tests for per-run log rotation in :mod:`pip_agent.logging_setup`.

The rotation contract is the load-bearing piece: every boot renames
``pip-boy.log`` → ``pip-boy.1.log``, the old ``.1`` → ``.2``, and the
old ``.2`` is discarded. Handler attachment is covered separately
via a smoke test that confirms records written to the root logger
actually land in the file.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pip_agent.logging_setup import (
    LOG_FILENAME,
    LOG_KEEP_BACKUPS,
    install_file_logging,
    rotate_logs,
)

# ---------------------------------------------------------------------------
# rotate_logs — pure filesystem side effect
# ---------------------------------------------------------------------------


def _log_dir(workdir: Path) -> Path:
    return workdir / ".pip" / "log"


def test_rotate_creates_log_dir_when_missing(tmp_path: Path) -> None:
    log_dir = _log_dir(tmp_path)
    assert not log_dir.exists()
    rotate_logs(log_dir)
    assert log_dir.is_dir()


def test_rotate_is_noop_when_no_logs_exist(tmp_path: Path) -> None:
    log_dir = _log_dir(tmp_path)
    rotate_logs(log_dir)
    assert list(log_dir.iterdir()) == []


def test_rotate_shifts_current_to_backup_1(tmp_path: Path) -> None:
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / LOG_FILENAME).write_text("run-N", encoding="utf-8")

    rotate_logs(log_dir)

    assert not (log_dir / LOG_FILENAME).exists(), (
        "current file must have been moved out of the way"
    )
    assert (log_dir / "pip-boy.1.log").read_text(encoding="utf-8") == "run-N"


def test_rotate_shifts_chain_and_drops_oldest(tmp_path: Path) -> None:
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / LOG_FILENAME).write_text("run-3", encoding="utf-8")
    (log_dir / "pip-boy.1.log").write_text("run-2", encoding="utf-8")
    (log_dir / "pip-boy.2.log").write_text("run-1", encoding="utf-8")

    rotate_logs(log_dir)

    assert not (log_dir / LOG_FILENAME).exists()
    assert (log_dir / "pip-boy.1.log").read_text(encoding="utf-8") == "run-3"
    assert (log_dir / "pip-boy.2.log").read_text(encoding="utf-8") == "run-2"
    # run-1 must have been discarded — LOG_KEEP_BACKUPS + current is
    # the full retention budget (3 files total).
    files = sorted(p.name for p in log_dir.iterdir())
    assert files == ["pip-boy.1.log", "pip-boy.2.log"]


def test_keep_backups_invariant() -> None:
    # Pin the retention budget. Loosening this is a deliberate product
    # choice; tightening it silently would drop debugging history.
    assert LOG_KEEP_BACKUPS == 2


def test_multiple_rotations_preserve_chain(tmp_path: Path) -> None:
    """Five rounds of (write + rotate) end up with exactly 2 backups."""
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)

    for i in range(5):
        (log_dir / LOG_FILENAME).write_text(f"run-{i}", encoding="utf-8")
        rotate_logs(log_dir)

    files = sorted(p.name for p in log_dir.iterdir())
    assert files == ["pip-boy.1.log", "pip-boy.2.log"]
    # Most recent written run became .1; the one before that is .2.
    assert (log_dir / "pip-boy.1.log").read_text(encoding="utf-8") == "run-4"
    assert (log_dir / "pip-boy.2.log").read_text(encoding="utf-8") == "run-3"


# ---------------------------------------------------------------------------
# install_file_logging — integration with the root logger
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_root() -> logging.Logger:
    """Snapshot + restore root logger state per test.

    ``install_file_logging`` adds a handler to the root logger, which
    would leak between tests and noisily spam pytest's capture. We
    save / restore around each case.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    # Root at DEBUG so the fresh handler actually sees the test emit.
    root.setLevel(logging.DEBUG)
    yield root
    # Close any handlers this test added so Windows lets us delete the file.
    for h in root.handlers[:]:
        try:
            h.close()
        except Exception:
            pass
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_install_rotates_then_creates_fresh_current(
    tmp_path: Path, isolated_root: logging.Logger,
) -> None:
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / LOG_FILENAME).write_text("previous run", encoding="utf-8")

    install_file_logging(tmp_path)

    assert (log_dir / "pip-boy.1.log").read_text(encoding="utf-8") == "previous run"
    assert (log_dir / LOG_FILENAME).exists()
    # Current log starts empty (mode="w"); a later emit fills it.
    assert (log_dir / LOG_FILENAME).read_text(encoding="utf-8") == ""


def test_install_attaches_handler_to_root(
    tmp_path: Path, isolated_root: logging.Logger,
) -> None:
    handler = install_file_logging(tmp_path)
    assert handler in isolated_root.handlers


def test_emitted_records_reach_the_file(
    tmp_path: Path, isolated_root: logging.Logger,
) -> None:
    handler = install_file_logging(tmp_path)

    logging.getLogger("pip_agent.host").info("boot complete")
    handler.flush()

    log_text = (_log_dir(tmp_path) / LOG_FILENAME).read_text(encoding="utf-8")
    assert "boot complete" in log_text
    assert "pip_agent.host" in log_text
    assert "INFO" in log_text


def test_format_matches_console_layout(
    tmp_path: Path, isolated_root: logging.Logger,
) -> None:
    # The file format string is meant to be identical to the console
    # formatter so a dump reads exactly like a live tail.
    handler = install_file_logging(tmp_path)
    logging.getLogger("pip_agent").warning("channel flaky")
    handler.flush()

    line = (
        _log_dir(tmp_path) / LOG_FILENAME
    ).read_text(encoding="utf-8").strip().splitlines()[0]
    # Default ``%(asctime)s`` is ``YYYY-MM-DD HH:MM:SS,mmm`` (two
    # whitespace-separated tokens), followed by level / name / message.
    parts = line.split(" ", 4)
    assert len(parts) == 5
    assert parts[2] == "WARNING"
    assert parts[3] == "pip_agent"
    assert parts[4] == "channel flaky"
