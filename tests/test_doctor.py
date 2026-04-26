"""Tests for ``pip-boy doctor``.

The doctor is a *report*: deterministic shape, no side effects, no
boot dependencies. These tests therefore cover behaviour rather than
exact wording — the wording will evolve as new sections land in
later phases.

Coverage:

* Each of the five top-level sections renders, in order.
* Forcing ``--no-tui`` short-circuits the capability ladder at
  ``user_optout`` (this is the operator's quick "is the env still
  set up the way I think it is" smoke test).
* The themes section enumerates both built-ins, marks the active
  one, and surfaces a broken local theme as an issue.
* The capability log section reads the most recent entries and
  returns a clean placeholder when the file is missing.
* Tolerance: a malformed line in the capability log doesn't crash
  the doctor (the diagnostic must survive a half-broken env).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from pip_agent.doctor import render_to_string, run_doctor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_VALID_PALETTE = {
    "background": "#000000",
    "foreground": "#ffffff",
    "accent": "#00ff00",
    "accent_dim": "#003300",
    "user_input": "#ffffff",
    "agent_text": "#ffffff",
    "thinking": "#888888",
    "tool_call": "#88ddff",
    "log_info": "#ffffff",
    "log_warning": "#ffcc66",
    "log_error": "#ff6666",
    "status_bar": "#222222",
    "status_bar_text": "#ffffff",
}


def _write_local_theme(workspace: Path, slug: str) -> None:
    theme_dir = workspace / ".pip" / "themes" / slug
    theme_dir.mkdir(parents=True, exist_ok=True)
    palette_block = "\n".join(
        f'{k} = "{v}"' for k, v in _VALID_PALETTE.items()
    )
    (theme_dir / "theme.toml").write_text(
        "\n".join(
            [
                "[theme]",
                f'name = "{slug}"',
                f'display_name = "{slug.title()}"',
                'version = "0.1.0"',
                'author = "test"',
                'description = "fixture theme"',
                "show_art = true",
                "show_app_log = true",
                "show_status_bar = true",
                "",
                "[palette]",
                palette_block,
                "",
            ]
        ),
        encoding="utf-8",
    )
    (theme_dir / "theme.tcss").write_text(
        "Screen { background: $surface; }\n", encoding="utf-8",
    )


def _write_broken_local_theme(workspace: Path, slug: str) -> None:
    theme_dir = workspace / ".pip" / "themes" / slug
    theme_dir.mkdir(parents=True, exist_ok=True)
    (theme_dir / "theme.toml").write_text(
        "this isn't toml = =\n", encoding="utf-8",
    )
    (theme_dir / "theme.tcss").write_text(
        "Screen { background: black; }\n", encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    pip_dir = tmp_path / ".pip"
    pip_dir.mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Section / shape
# ---------------------------------------------------------------------------


class TestSections:
    def test_all_sections_present_in_order(self, workspace: Path) -> None:
        report = render_to_string(workdir=workspace, force_no_tui=True)
        order = [
            "Pip-Boy doctor",
            "Versions",
            "Locale & console",
            "TUI capability ladder",
            "Themes",
            "Recent capability log",
        ]
        positions = []
        for heading in order:
            idx = report.find(heading)
            assert idx >= 0, f"missing section: {heading!r}"
            positions.append(idx)
        assert positions == sorted(positions), (
            f"sections out of order: {positions}"
        )

    def test_versions_section_lists_python_and_textual(
        self, workspace: Path,
    ) -> None:
        report = render_to_string(workdir=workspace, force_no_tui=True)
        assert "python" in report
        assert "textual" in report
        assert "rich" in report

    def test_run_doctor_returns_zero(self, workspace: Path) -> None:
        rc = run_doctor(
            workdir=workspace, force_no_tui=True, out=io.StringIO(),
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# Capability ladder
# ---------------------------------------------------------------------------


class TestCapabilityLadder:
    def test_force_no_tui_short_circuits_at_user_optout(
        self, workspace: Path,
    ) -> None:
        report = render_to_string(workdir=workspace, force_no_tui=True)
        assert "stage=user_optout" in report
        assert "FALLBACK" in report
        assert "operator passed --no-tui" in report

    def test_capability_ladder_shows_each_stage_marker(
        self, workspace: Path,
    ) -> None:
        # Without --no-tui in a non-TTY pytest session, the ladder
        # fails at the tty stage. Either FAIL or PASS markers are
        # acceptable; what matters is that the section is structured.
        report = render_to_string(workdir=workspace, force_no_tui=False)
        assert "stages:" in report
        assert "[FAIL]" in report or "[PASS]" in report


# ---------------------------------------------------------------------------
# Themes section
# ---------------------------------------------------------------------------


class TestThemes:
    def test_lists_builtin_themes_and_marks_active(
        self, workspace: Path,
    ) -> None:
        report = render_to_string(workdir=workspace, force_no_tui=True)
        assert "wasteland" in report
        assert "vault-amber" in report
        # Active theme (default = wasteland) must be visibly marked.
        assert "wasteland *" in report or "wasteland*" in report

    def test_local_override_appears_with_local_origin(
        self, workspace: Path,
    ) -> None:
        _write_local_theme(workspace, "wasteland")
        report = render_to_string(workdir=workspace, force_no_tui=True)
        assert "[local] wasteland" in report
        # The builtin entry with the same slug must NOT appear once
        # the local copy has won the precedence chain.
        assert "[builtin] wasteland" not in report

    def test_broken_local_theme_listed_in_issues(
        self, workspace: Path,
    ) -> None:
        _write_broken_local_theme(workspace, "broken")
        report = render_to_string(workdir=workspace, force_no_tui=True)
        assert "issues" in report
        assert "broken" in report


# ---------------------------------------------------------------------------
# Recent capability log
# ---------------------------------------------------------------------------


class TestCapabilityLog:
    def test_missing_log_renders_placeholder(self, workspace: Path) -> None:
        report = render_to_string(workdir=workspace, force_no_tui=True)
        assert "(empty" in report

    def test_recent_entries_render_newest_first(
        self, workspace: Path,
    ) -> None:
        log_path = workspace / ".pip" / "tui_capability.log"
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "ok": True, "stage": "ready",
             "detail": "all stages passed", "checks": []},
            {"ts": "2026-01-02T00:00:00Z", "ok": False, "stage": "tty",
             "detail": "stdin not a TTY", "checks": []},
        ]
        log_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n",
            encoding="utf-8",
        )
        report = render_to_string(workdir=workspace, force_no_tui=True)

        idx_old = report.find("2026-01-01T00:00:00Z")
        idx_new = report.find("2026-01-02T00:00:00Z")
        assert idx_old > 0 and idx_new > 0
        # Newest first: the later timestamp must appear before the
        # earlier one so a regression streak is obvious at the top.
        assert idx_new < idx_old

    def test_malformed_log_line_does_not_crash(
        self, workspace: Path,
    ) -> None:
        log_path = workspace / ".pip" / "tui_capability.log"
        log_path.write_text(
            "this is not json\n"
            + json.dumps(
                {"ts": "2026-01-03T00:00:00Z", "ok": True, "stage": "ready",
                 "detail": "all stages passed", "checks": []}
            ) + "\n",
            encoding="utf-8",
        )
        report = render_to_string(workdir=workspace, force_no_tui=True)
        assert "[malformed]" in report
        assert "2026-01-03T00:00:00Z" in report
