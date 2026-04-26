"""Phase B.1 — ThemeManager.discover dual-path loading.

The four canonical fixtures exercised here come straight out of the
plan ("builtin/local/重名/broken 四种 fixture"):

* **builtin only** — package ships valid themes, workspace is empty.
* **local only** — workspace ``.pip/themes`` has an extra theme; the
  builtin set is intentionally pruned via an isolated builtin root so
  the assertion is over a small known set rather than everything that
  later phases keep adding to ``src/pip_agent/tui/themes``.
* **override** — local theme with the same slug as a builtin replaces
  the builtin while the override fact is logged at INFO.
* **broken** — a malformed local theme must NOT crash discovery; it
  ends up on :attr:`ThemeDiscovery.issues`, sibling themes still load.

The tests construct an *isolated* builtin root for the override and
broken cases so we don't depend on whichever themes happen to ship at
the time the test runs.
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
# Fixtures — isolated builtin/local roots with known content.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_builtin(tmp_path: Path) -> Path:
    root = tmp_path / "builtin_themes"
    root.mkdir()
    _write_theme(root, DEFAULT_THEME_NAME, display_name="Wasteland (test)")
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A fresh workspace root; ``.pip/themes`` does not yet exist."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# Builtin-only — local root absent.
# ---------------------------------------------------------------------------


def test_builtin_only_when_local_dir_missing(
    isolated_builtin: Path, workspace: Path,
) -> None:
    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    snap = mgr.discover()

    assert set(snap.bundles) == {DEFAULT_THEME_NAME}
    assert snap.bundles[DEFAULT_THEME_NAME].source == f"builtin:{DEFAULT_THEME_NAME}"
    assert snap.builtin_count == 1
    assert snap.local_count == 0
    assert snap.issues == ()


def test_builtin_only_when_local_dir_empty(
    isolated_builtin: Path, workspace: Path,
) -> None:
    (workspace / ".pip" / "themes").mkdir(parents=True)
    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    snap = mgr.discover()

    assert set(snap.bundles) == {DEFAULT_THEME_NAME}
    assert snap.local_count == 0


# ---------------------------------------------------------------------------
# Local-only contributions — local-named-themes show up alongside builtins.
# ---------------------------------------------------------------------------


def test_local_themes_added_alongside_builtins(
    isolated_builtin: Path, workspace: Path,
) -> None:
    local_root = workspace / ".pip" / "themes"
    local_root.mkdir(parents=True)
    _write_theme(local_root, "amber-console", display_name="Amber Console")

    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    snap = mgr.discover()

    assert set(snap.bundles) == {DEFAULT_THEME_NAME, "amber-console"}
    assert snap.bundles["amber-console"].source == "local:amber-console"
    assert snap.builtin_count == 1
    assert snap.local_count == 1
    assert snap.issues == ()


# ---------------------------------------------------------------------------
# Override — local replaces builtin when slugs collide.
# ---------------------------------------------------------------------------


def test_local_overrides_builtin_with_same_slug(
    isolated_builtin: Path,
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    local_root = workspace / ".pip" / "themes"
    local_root.mkdir(parents=True)
    _write_theme(
        local_root, DEFAULT_THEME_NAME, display_name="Wasteland (overridden)",
    )

    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    with caplog.at_level(logging.INFO, logger="pip_agent.tui.manager"):
        snap = mgr.discover()

    bundle = snap.bundles[DEFAULT_THEME_NAME]
    assert bundle.source == f"local:{DEFAULT_THEME_NAME}", (
        "local entry must replace builtin entry of the same name"
    )
    assert bundle.manifest.display_name == "Wasteland (overridden)"
    assert any(
        "overrides builtin" in rec.getMessage() for rec in caplog.records
    ), "override must log an INFO line so operators see it"


# ---------------------------------------------------------------------------
# Broken theme — must NOT crash boot.
# ---------------------------------------------------------------------------


def test_broken_local_theme_is_skipped_with_warning(
    isolated_builtin: Path,
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    local_root = workspace / ".pip" / "themes"
    local_root.mkdir(parents=True)

    healthy = _write_theme(local_root, "healthy")
    broken = local_root / "broken"
    broken.mkdir()
    (broken / "theme.toml").write_text("not = valid = toml", encoding="utf-8")

    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    with caplog.at_level(logging.WARNING, logger="pip_agent.tui.manager"):
        snap = mgr.discover()

    assert "healthy" in snap.bundles, "healthy sibling must still load"
    assert "broken" not in snap.bundles, "broken theme must be skipped"
    assert any(
        issue.path == broken and issue.origin == "local"
        for issue in snap.issues
    ), "broken theme must surface as a ThemeLoadIssue"
    assert any(
        "Skipping broken local theme" in rec.getMessage()
        for rec in caplog.records
    )
    assert healthy.exists()  # sanity for the helper


def test_missing_manifest_is_skipped(
    isolated_builtin: Path, workspace: Path,
) -> None:
    local_root = workspace / ".pip" / "themes"
    local_root.mkdir(parents=True)
    no_manifest = local_root / "no-manifest"
    no_manifest.mkdir()
    (no_manifest / "theme.tcss").write_text("Screen {}\n", encoding="utf-8")

    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    snap = mgr.discover()

    assert "no-manifest" not in snap.bundles
    assert any(issue.path == no_manifest for issue in snap.issues)


def test_directory_name_must_match_manifest_name(
    isolated_builtin: Path, workspace: Path,
) -> None:
    local_root = workspace / ".pip" / "themes"
    local_root.mkdir(parents=True)
    _write_theme(
        local_root,
        "wrong-dir",
        manifest_name_override="other-name",
    )

    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    snap = mgr.discover()

    assert "wrong-dir" not in snap.bundles
    assert "other-name" not in snap.bundles
    assert any(issue.path.name == "wrong-dir" for issue in snap.issues)


def test_hidden_and_non_dir_entries_are_ignored(
    isolated_builtin: Path, workspace: Path,
) -> None:
    local_root = workspace / ".pip" / "themes"
    local_root.mkdir(parents=True)
    (local_root / "README.md").write_text("placeholder", encoding="utf-8")
    (local_root / ".gitkeep").write_text("", encoding="utf-8")
    (local_root / ".hidden").mkdir()
    (local_root / ".hidden" / "theme.toml").write_text(
        "garbage", encoding="utf-8",
    )

    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    snap = mgr.discover()

    assert set(snap.bundles) == {DEFAULT_THEME_NAME}
    assert snap.issues == ()


# ---------------------------------------------------------------------------
# resolve()/get() lookup contract.
# ---------------------------------------------------------------------------


def test_resolve_returns_default_when_requested_missing(
    isolated_builtin: Path,
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    with caplog.at_level(logging.WARNING, logger="pip_agent.tui.manager"):
        bundle = mgr.resolve("does-not-exist")
    assert bundle.manifest.name == DEFAULT_THEME_NAME
    assert any(
        "not found" in rec.getMessage() for rec in caplog.records
    )


def test_resolve_returns_requested_when_present(
    isolated_builtin: Path, workspace: Path,
) -> None:
    local_root = workspace / ".pip" / "themes"
    local_root.mkdir(parents=True)
    _write_theme(local_root, "amber-console")

    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    bundle = mgr.resolve("amber-console")
    assert bundle.manifest.name == "amber-console"


def test_resolve_raises_when_default_missing(tmp_path: Path) -> None:
    empty = tmp_path / "no_builtins"
    empty.mkdir()
    mgr = ThemeManager(builtin_root=empty, workdir=None)
    with pytest.raises(LookupError, match="(?i)default theme"):
        mgr.resolve(None)


def test_get_lazily_discovers_on_first_call(
    isolated_builtin: Path, workspace: Path,
) -> None:
    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    bundle = mgr.get(DEFAULT_THEME_NAME)
    assert bundle is not None
    assert bundle.source == f"builtin:{DEFAULT_THEME_NAME}"


def test_local_root_path_is_exposed(
    isolated_builtin: Path, workspace: Path,
) -> None:
    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=workspace)
    assert mgr.local_root == workspace / ".pip" / "themes"


def test_local_root_is_none_when_workdir_unset(
    isolated_builtin: Path,
) -> None:
    mgr = ThemeManager(builtin_root=isolated_builtin, workdir=None)
    assert mgr.local_root is None


# ---------------------------------------------------------------------------
# load_theme_bundle — the underlying loader exposed to ``runner.build_app``.
# ---------------------------------------------------------------------------


def test_load_theme_bundle_clamps_oversized_art(tmp_path: Path) -> None:
    long_line = "X" * 80
    art = "\n".join([long_line] * 12)
    theme_dir = _write_theme(tmp_path, "art-test", art=art)

    bundle = load_theme_bundle(theme_dir, origin="builtin")

    assert bundle.art_truncated is True
    rows = bundle.art.splitlines()
    assert len(rows) <= 8
    assert all(len(row) <= 32 for row in rows)


def test_load_theme_bundle_rejects_directory_name_mismatch(
    tmp_path: Path,
) -> None:
    theme_dir = _write_theme(
        tmp_path, "dir-name", manifest_name_override="other-name",
    )
    with pytest.raises(ThemeValidationError, match="does not match"):
        load_theme_bundle(theme_dir, origin="builtin")
