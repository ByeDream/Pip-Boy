"""Built-in theme loader (Phase A scope).

Reads a single named theme from :mod:`pip_agent.tui.themes` and
returns a fully validated :class:`ThemeBundle`. Phase B replaces the
direct call site with a ``ThemeManager.discover()`` walker that also
handles ``<workspace>/.pip/themes/`` and the broken-theme degradation
path; until then, callers ask for ``"wasteland"`` by name.

A separate module (rather than baking the loader into ``__init__``)
keeps the import graph clean: the theme API contract module
(``theme_api.py``) stays free of filesystem I/O so it remains
trivially unit-testable.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from pip_agent.tui.theme_api import (
    ThemeBundle,
    ThemeValidationError,
    clamp_art,
    validate_manifest_dict,
)
from pip_agent.tui.themes import BUILTIN_THEMES_DIR

log = logging.getLogger(__name__)

__all__ = ["load_builtin_theme"]


def load_builtin_theme(name: str) -> ThemeBundle:
    """Load and validate a builtin theme by slug.

    Raises :class:`pip_agent.tui.theme_api.ThemeValidationError` when
    the manifest fails the v1 schema check, :class:`FileNotFoundError`
    when the theme directory is missing, and :class:`OSError` for
    other I/O errors. Phase B's ``ThemeManager.discover()`` will catch
    these for the local-themes path; the builtin path is shipped in
    the package, so a failure here is a developer bug, not a user one,
    and the host SHOULD crash so the regression is caught in CI.
    """
    theme_dir = BUILTIN_THEMES_DIR / name
    if not theme_dir.is_dir():
        raise FileNotFoundError(f"Builtin theme '{name}' not found at {theme_dir}")

    manifest_path = theme_dir / "theme.toml"
    tcss_path = theme_dir / "theme.tcss"
    art_path = theme_dir / "art.txt"

    with manifest_path.open("rb") as fh:
        raw = tomllib.load(fh)
    manifest = validate_manifest_dict(raw, where=str(manifest_path))

    if manifest.name != name:
        raise ThemeValidationError(
            f"{manifest_path}: manifest name='{manifest.name}' does not match "
            f"directory name='{name}'"
        )

    tcss = tcss_path.read_text(encoding="utf-8") if tcss_path.exists() else ""

    art_text = ""
    art_truncated = False
    if manifest.show_art and art_path.exists():
        raw_art = art_path.read_text(encoding="utf-8")
        art_text, art_truncated = clamp_art(raw_art)
        if art_truncated:
            log.warning(
                "Theme '%s' art exceeds 32x8 limit; truncated.", name
            )

    return ThemeBundle(
        manifest=manifest,
        tcss=tcss,
        art=art_text,
        source=f"builtin:{name}",
        art_truncated=art_truncated,
    )
