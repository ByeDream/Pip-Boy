"""SVG baseline snapshots for the TUI default layout, banner, and /help.

Uses ``pytest-textual-snapshot`` to render each scenario into an SVG
and diff against a tracked baseline. New baselines are produced via
``pytest --snapshot-update tests/test_tui_snapshots.py``; reviewers
inspect the SVGs visually before merging.

Three scenarios pinned in Phase A:

* ``default_layout`` — empty TUI right after mount.
* ``boot_banner``    — first interactive frame (status bar + banner +
  channel-ready + ready hint visible).
* ``help_response``  — a multi-line markdown reply rendered in the
  agent pane.

Phase B will add a parallel set against the second built-in theme so
the baselines double as visual proof that swapping the theme really
does change *only* the appearance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ``snap_compare`` is the fixture from pytest-textual-snapshot; we
# import lazily so the rest of the suite works even if the plugin
# isn't available locally.
pytest.importorskip("pytest_textual_snapshot")


_SCENARIOS_DIR = Path(__file__).parent / "tui_snapshot_apps"


def test_default_layout_snapshot(snap_compare):
    """Empty TUI right after mount — locks topology + theme tokens."""
    assert snap_compare(
        str(_SCENARIOS_DIR / "default_layout.py"),
        terminal_size=(100, 30),
    )


def test_boot_banner_snapshot(snap_compare):
    """Banner + channel-ready + ready hint rendered after boot."""
    assert snap_compare(
        str(_SCENARIOS_DIR / "boot_banner.py"),
        terminal_size=(100, 30),
    )


def test_help_response_snapshot(snap_compare):
    """Multi-line plain-text response in the agent pane (``/help``)."""
    assert snap_compare(
        str(_SCENARIOS_DIR / "help_response.py"),
        terminal_size=(100, 30),
    )


def test_default_layout_vault_amber_snapshot(snap_compare):
    """Empty TUI under ``vault-amber`` — proves topology is theme-invariant.

    Diffing this against ``default_layout`` should show *only* color +
    border changes. Any IDs that move, sizes that shift, or widgets
    that appear/disappear flag a theme that broke out of its
    appearance-only contract.
    """
    assert snap_compare(
        str(_SCENARIOS_DIR / "default_layout_vault_amber.py"),
        terminal_size=(100, 30),
    )
