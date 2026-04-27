"""``ThemeManager.discover`` against the single workspace themes root.

The manager now scans exactly one directory (``<workspace>/.pip/themes/``);
built-in themes are seeded into that directory by the scaffold. These
tests cover:

* **empty workspace** — no themes, no issues.
* **populated workspace** — valid themes load, slug → bundle mapping is
  correct.
* **broken theme** — a malformed manifest ends up on ``issues`` and
  siblings still load.
* **hidden files / non-directories** — README.md, .gitkeep and hidden
  dirs are ignored.
* **lookup contract** — ``get`` / ``resolve`` fall back to the default
  slug with a WARNING on miss, raise ``LookupError`` when even the
  default is missing (empty workspace).
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from pip_agent.tui.manager import (
    DEFAULT_THEME_NAME,
    ThemeManager,
    load_theme_bundle,
)
from pip_agent.tui.theme_api import ThemeValidationError

# ---------------------------------------------------------------------------
# Helpers — build minimal-but-valid theme directories on disk.
# ---------------------------------------------------------------------------


_VALID_PALETTE = textwrap.dedent(
    """
    background = "#000000"
    foreground = "#ffffff"
    accent = "#7CFC00"
    accent_dim = "#3a8000"
    user_input = "#aaffaa"
    agent_text = "#7CFC00"
    thinking = "#888888"
    tool_call = "#88ddff"
    log_info = "#7CFC00"
    log_warning = "#ffcc66"
    log_error = "#ff6666"
    status_bar = "#101010"
    status_bar_text = "#ffffff"
    """
).strip()


def _write_theme(
    root: Path,
    name: str,
    *,
    display_name: str | None = None,
    art: str | None = None,
    show_art: bool = True,
    extra_theme_lines: str = "",
    palette: str | None = None,
    manifest_name_override: str | None = None,
) -> Path:
    """Write a valid theme directory under ``root/<name>/``.

    Returns the theme directory path so callers can poke individual
    files (e.g. truncate ``theme.toml`` to simulate corruption).
    """
    theme_dir = root / name
    theme_dir.mkdir(parents=True, exist_ok=True)
    manifest_name = manifest_name_override or name
    toml = textwrap.dedent(
        f"""
        [theme]
        name = "{manifest_name}"
        display_name = "{display_name or name.title()}"
        version = "0.1.0"
        author = "test"
        description = "fixture theme {manifest_name}"
        show_art = {str(show_art).lower()}
        {extra_theme_lines}

        [palette]
        {palette or _VALID_PALETTE}
        """
    ).strip()
    (theme_dir / "theme.toml").write_text(toml + "\n", encoding="utf-8")
    (theme_dir / "theme.tcss").write_text(
        "Screen {{ background: $surface; }}\n", encoding="utf-8"
    )
    if art is not None:
        (theme_dir / "art.txt").write_text(art, encoding="utf-8")
    return theme_dir


# ---------------------------------------------------------------------------
# Fixtures — populated workspace roots.
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A fresh workspace root; ``.pip/themes`` does not yet exist."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def seeded_workspace(workspace: Path) -> Path:
    """Workspace with one valid theme at ``.pip/themes/<DEFAULT>/``."""
    _write_theme(
        workspace / ".pip" / "themes",
        DEFAULT_THEME_NAME,
        display_name="Wasteland (test)",
    )
    return workspace


# ---------------------------------------------------------------------------
# Empty workspace — no crash, no phantom themes.
# ---------------------------------------------------------------------------


def test_empty_workspace_discovers_nothing(workspace: Path) -> None:
    mgr = ThemeManager(workdir=workspace)
    snap = mgr.discover()

    assert snap.bundles == {}
    assert snap.count == 0
    assert snap.issues == ()


def test_missing_themes_dir_is_fine(workspace: Path) -> None:
    # .pip exists but .pip/themes does not — fresh scaffold pre-first-boot
    (workspace / ".pip").mkdir()
    mgr = ThemeManager(workdir=workspace)
    snap = mgr.discover()

    assert snap.bundles == {}
    assert snap.count == 0


def test_no_workdir_means_empty_snapshot() -> None:
    mgr = ThemeManager(workdir=None)
    snap = mgr.discover()

    assert snap.bundles == {}
    assert snap.count == 0


# ---------------------------------------------------------------------------
# Populated workspace — themes show up under workspace paths.
# ---------------------------------------------------------------------------


def test_themes_load_from_workspace_root(workspace: Path) -> None:
    themes_root = workspace / ".pip" / "themes"
    _write_theme(themes_root, DEFAULT_THEME_NAME, display_name="Wasteland")
    _write_theme(themes_root, "amber-console", display_name="Amber Console")

    mgr = ThemeManager(workdir=workspace)
    snap = mgr.discover()

    assert set(snap.bundles) == {DEFAULT_THEME_NAME, "amber-console"}
    assert snap.count == 2
    assert snap.issues == ()

    amber = snap.bundles["amber-console"]
    assert amber.path == themes_root / "amber-console"
    assert amber.manifest.display_name == "Amber Console"


# ---------------------------------------------------------------------------
# Broken theme — must NOT crash boot.
# ---------------------------------------------------------------------------


def test_broken_theme_is_skipped_with_warning(
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    themes_root = workspace / ".pip" / "themes"
    healthy = _write_theme(themes_root, "healthy")
    broken = themes_root / "broken"
    broken.mkdir()
    (broken / "theme.toml").write_text("not = valid = toml", encoding="utf-8")

    mgr = ThemeManager(workdir=workspace)
    with caplog.at_level(logging.WARNING, logger="pip_agent.tui.manager"):
        snap = mgr.discover()

    assert "healthy" in snap.bundles
    assert "broken" not in snap.bundles
    assert any(issue.path == broken for issue in snap.issues)
    assert any(
        "Skipping broken theme" in rec.getMessage() for rec in caplog.records
    )
    assert healthy.exists()


def test_missing_manifest_is_skipped(workspace: Path) -> None:
    themes_root = workspace / ".pip" / "themes"
    themes_root.mkdir(parents=True)
    no_manifest = themes_root / "no-manifest"
    no_manifest.mkdir()
    (no_manifest / "theme.tcss").write_text("Screen {}\n", encoding="utf-8")

    mgr = ThemeManager(workdir=workspace)
    snap = mgr.discover()

    assert "no-manifest" not in snap.bundles
    assert any(issue.path == no_manifest for issue in snap.issues)


def test_directory_name_must_match_manifest_name(workspace: Path) -> None:
    themes_root = workspace / ".pip" / "themes"
    _write_theme(
        themes_root,
        "wrong-dir",
        manifest_name_override="other-name",
    )

    mgr = ThemeManager(workdir=workspace)
    snap = mgr.discover()

    assert "wrong-dir" not in snap.bundles
    assert "other-name" not in snap.bundles
    assert any(issue.path.name == "wrong-dir" for issue in snap.issues)


def test_hidden_and_non_dir_entries_are_ignored(
    seeded_workspace: Path,
) -> None:
    themes_root = seeded_workspace / ".pip" / "themes"
    (themes_root / "README.md").write_text("placeholder", encoding="utf-8")
    (themes_root / ".gitkeep").write_text("", encoding="utf-8")
    (themes_root / ".hidden").mkdir()
    (themes_root / ".hidden" / "theme.toml").write_text(
        "garbage", encoding="utf-8",
    )

    mgr = ThemeManager(workdir=seeded_workspace)
    snap = mgr.discover()

    assert set(snap.bundles) == {DEFAULT_THEME_NAME}
    assert snap.issues == ()


# ---------------------------------------------------------------------------
# snapshot() vs discover() — cache contract.
# ---------------------------------------------------------------------------


def test_snapshot_is_none_before_first_discover(workspace: Path) -> None:
    mgr = ThemeManager(workdir=workspace)
    assert mgr.snapshot() is None


def test_snapshot_returns_last_discovery(seeded_workspace: Path) -> None:
    mgr = ThemeManager(workdir=seeded_workspace)
    first = mgr.discover()
    cached = mgr.snapshot()
    assert cached is first


def test_discover_rewalks_filesystem(workspace: Path) -> None:
    themes_root = workspace / ".pip" / "themes"
    _write_theme(themes_root, "first")

    mgr = ThemeManager(workdir=workspace)
    snap1 = mgr.discover()
    assert set(snap1.bundles) == {"first"}

    _write_theme(themes_root, "second")
    snap2 = mgr.discover()
    assert set(snap2.bundles) == {"first", "second"}


# ---------------------------------------------------------------------------
# resolve() / get() lookup contract.
# ---------------------------------------------------------------------------


def test_resolve_returns_default_when_requested_missing(
    seeded_workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mgr = ThemeManager(workdir=seeded_workspace)
    with caplog.at_level(logging.WARNING, logger="pip_agent.tui.manager"):
        bundle = mgr.resolve("does-not-exist")
    assert bundle.manifest.name == DEFAULT_THEME_NAME
    assert any("not found" in rec.getMessage() for rec in caplog.records)


def test_resolve_returns_requested_when_present(workspace: Path) -> None:
    themes_root = workspace / ".pip" / "themes"
    _write_theme(themes_root, DEFAULT_THEME_NAME)
    _write_theme(themes_root, "amber-console")

    mgr = ThemeManager(workdir=workspace)
    bundle = mgr.resolve("amber-console")
    assert bundle.manifest.name == "amber-console"


def test_resolve_raises_when_default_missing(tmp_path: Path) -> None:
    # Empty workspace — no themes at all, not even the default
    mgr = ThemeManager(workdir=tmp_path)
    with pytest.raises(LookupError, match="(?i)default theme"):
        mgr.resolve(None)


def test_get_lazily_discovers_on_first_call(seeded_workspace: Path) -> None:
    mgr = ThemeManager(workdir=seeded_workspace)
    bundle = mgr.get(DEFAULT_THEME_NAME)
    assert bundle is not None
    assert bundle.path == (
        seeded_workspace / ".pip" / "themes" / DEFAULT_THEME_NAME
    )


def test_themes_root_path_is_exposed(workspace: Path) -> None:
    mgr = ThemeManager(workdir=workspace)
    assert mgr.themes_root == workspace / ".pip" / "themes"


def test_themes_root_is_none_when_workdir_unset() -> None:
    mgr = ThemeManager(workdir=None)
    assert mgr.themes_root is None


# ---------------------------------------------------------------------------
# load_theme_bundle — the underlying per-directory loader.
# ---------------------------------------------------------------------------


def test_load_theme_bundle_loads_ascii_art_frames(tmp_path: Path) -> None:
    theme_dir = _write_theme(tmp_path, "art-test")
    (theme_dir / "ascii_art_0.txt").write_text("hello\nworld", encoding="utf-8")
    (theme_dir / "ascii_art_1.txt").write_text("foo\nbar\nbaz", encoding="utf-8")

    bundle = load_theme_bundle(theme_dir)

    assert len(bundle.art_frames) == 2
    assert bundle.art_frames[0] == "hello\nworld"
    assert bundle.art_frames[1] == "foo\nbar\nbaz"
    assert bundle.art_frame_width == 5  # "hello"
    assert bundle.art_frame_height == 3  # 3 rows in frame 1


def test_load_theme_bundle_no_art_files_gives_empty_frames(tmp_path: Path) -> None:
    theme_dir = _write_theme(tmp_path, "no-art")

    bundle = load_theme_bundle(theme_dir)

    assert bundle.art_frames == ()
    assert bundle.art_frame_width == 0
    assert bundle.art_frame_height == 0


def test_load_theme_bundle_rejects_directory_name_mismatch(
    tmp_path: Path,
) -> None:
    theme_dir = _write_theme(
        tmp_path, "dir-name", manifest_name_override="other-name",
    )
    with pytest.raises(ThemeValidationError, match="does not match"):
        load_theme_bundle(theme_dir)


def test_load_theme_bundle_sets_path_field(tmp_path: Path) -> None:
    theme_dir = _write_theme(tmp_path, "path-test")
    bundle = load_theme_bundle(theme_dir)
    assert bundle.path == theme_dir
