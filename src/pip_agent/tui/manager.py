"""Theme discovery walker.

``ThemeManager`` is the single source of truth for which themes are
available at runtime. It walks one root — ``<workspace>/.pip/themes/``
— and nothing else.

Built-in themes are **seeded** into that directory by
:mod:`pip_agent.scaffold` on first boot; from the manager's point of
view there is no difference between a seeded example and a user-authored
theme. This removes the old "built-in vs local" dichotomy: everything
is workspace content, everything is editable, and operators can delete
any theme (including the ones pip-boy shipped with) without the
scaffold re-creating it.

Validation failures are tolerated: the walker logs a WARNING and
records a :class:`ThemeLoadIssue` so a single broken manifest never
wedges host boot. ``/theme list`` and ``pip-boy doctor`` surface the
skipped themes.

The manager is intentionally *not* a singleton. ``run_host`` constructs
exactly one and threads it through to ``host_commands`` and the TUI
runner; tests construct disposable instances per fixture. This keeps
filesystem state (``.pip/themes/``) testable without monkey-patching a
module-level cache.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pip_agent.tui.theme_api import (
    BANNER_MAX_COLUMNS,
    BANNER_MAX_ROWS,
    DECO_MAX_COLUMNS,
    DECO_MAX_ROWS,
    ThemeBundle,
    ThemeValidationError,
    clamp_art,
    clamp_banner,
    clamp_deco,
    validate_manifest_dict,
)

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_THEME_NAME",
    "ThemeDiscovery",
    "ThemeLoadIssue",
    "ThemeManager",
    "load_theme_bundle",
]


DEFAULT_THEME_NAME: str = "wasteland"
"""Slug of the theme used when the operator hasn't picked one.

Resolved at the bottom of the precedence chain (``host_state.json ->
default``). Defined here — not on the manager — because the host
needs the constant before it constructs the manager (e.g. when
reporting "theme not found" in ``pip-boy doctor``).
"""


_THEMES_SUBDIR = Path(".pip") / "themes"
"""Subpath inside the workspace where themes live.

A constant rather than a parameter because the path is part of the
user-facing contract documented in ``docs/themes.md``; tests still
override the *workspace* root, not this suffix.
"""


@dataclass(frozen=True, slots=True)
class ThemeLoadIssue:
    """A single broken theme the manager skipped during discovery.

    Surfaced via :attr:`ThemeDiscovery.issues` so ``pip-boy doctor`` and
    ``/theme list`` / ``/theme refresh`` can render a one-line
    ``(broken: …)`` hint instead of the broken theme silently
    disappearing.
    """

    path: Path
    """Theme directory the loader rejected (not the manifest path),
    so the user can ``ls`` it directly."""

    reason: str
    """Human-readable explanation; trimmed for log noise."""


def load_theme_bundle(theme_dir: Path) -> ThemeBundle:
    """Load and validate one theme directory into a :class:`ThemeBundle`.

    The function performs the *full* schema check (including the
    "manifest name matches directory name" invariant). Failures raise
    :class:`ThemeValidationError` (schema), :class:`FileNotFoundError`
    (missing directory), or the underlying :class:`OSError` /
    :class:`tomllib.TOMLDecodeError`. The discovery walker catches
    these so a single bad theme doesn't take down boot.
    """
    if not theme_dir.is_dir():
        raise FileNotFoundError(f"Theme directory not found: {theme_dir}")

    name = theme_dir.name
    manifest_path = theme_dir / "theme.toml"
    tcss_path = theme_dir / "theme.tcss"
    art_path = theme_dir / "art.txt"
    banner_path = theme_dir / "banner.txt"
    deco_path = theme_dir / "deco.txt"

    if not manifest_path.is_file():
        raise ThemeValidationError(
            f"{theme_dir}: missing required theme.toml"
        )

    with manifest_path.open("rb") as fh:
        raw = tomllib.load(fh)
    manifest = validate_manifest_dict(raw, where=str(manifest_path))

    if manifest.name != name:
        raise ThemeValidationError(
            f"{manifest_path}: manifest name='{manifest.name}' does not match "
            f"directory name='{name}'"
        )

    tcss = tcss_path.read_text(encoding="utf-8") if tcss_path.exists() else ""

    # Art assets: banner / deco are the v2 split; art.txt is the legacy
    # single block kept for backward compatibility. If a theme supplies
    # neither banner.txt nor art.txt the banner slot stays empty — the
    # TUI renders a blank top strip rather than error.
    art_text = ""
    art_truncated = False
    if manifest.show_art and art_path.exists():
        raw_art = art_path.read_text(encoding="utf-8")
        art_text, art_truncated = clamp_art(raw_art)
        if art_truncated:
            log.warning(
                "Theme '%s' art exceeds %dx%d limit; truncated.",
                name, 32, 8,
            )

    banner_text = ""
    banner_truncated = False
    if manifest.show_art and banner_path.exists():
        raw_banner = banner_path.read_text(encoding="utf-8")
        banner_text, banner_truncated = clamp_banner(raw_banner)
        if banner_truncated:
            log.warning(
                "Theme '%s' banner exceeds %dx%d limit; truncated.",
                name, BANNER_MAX_COLUMNS, BANNER_MAX_ROWS,
            )
    elif manifest.show_art and art_text:
        # Legacy theme: fall back to art.txt so the top strip has
        # something to draw without forcing theme authors to rename.
        banner_text = art_text

    deco_text = ""
    deco_truncated = False
    if manifest.show_art and deco_path.exists():
        raw_deco = deco_path.read_text(encoding="utf-8")
        deco_text, deco_truncated = clamp_deco(raw_deco)
        if deco_truncated:
            log.warning(
                "Theme '%s' deco exceeds %dx%d limit; truncated.",
                name, DECO_MAX_COLUMNS, DECO_MAX_ROWS,
            )

    return ThemeBundle(
        manifest=manifest,
        tcss=tcss,
        art=art_text,
        banner=banner_text,
        deco=deco_text,
        path=theme_dir,
        art_truncated=art_truncated,
        banner_truncated=banner_truncated,
        deco_truncated=deco_truncated,
    )


@dataclass(frozen=True, slots=True)
class ThemeDiscovery:
    """Result of one :meth:`ThemeManager.discover` pass.

    Returned (and cached) by the manager so tests can assert against an
    immutable snapshot rather than peeking at private state.
    """

    bundles: dict[str, ThemeBundle]
    """Slug → bundle."""

    issues: tuple[ThemeLoadIssue, ...]
    """All themes that failed validation, in scan order."""

    count: int
    """How many *valid* themes were found. ``pip-boy doctor`` surfaces
    this so ``0 themes discovered`` is a visible smoke signal that the
    scaffold never ran (or the operator deleted everything)."""


class ThemeManager:
    """Discover + look up themes from the workspace themes directory.

    The manager is constructed once per host boot; ``discover()`` is
    called eagerly so by the time the TUI builds its App, the active
    theme is already validated. ``/theme refresh`` calls it again to
    pick up on-disk edits without a restart.

    ``workdir`` is overridable; pass ``None`` to skip the scan entirely
    (used by the very early ``doctor`` bootstrap before ``WORKDIR`` is
    resolved, and by unit tests that want an empty manager).
    """

    def __init__(self, *, workdir: Path | None = None) -> None:
        self._workdir = workdir
        self._discovery: ThemeDiscovery | None = None

    @property
    def themes_root(self) -> Path | None:
        """Where themes live, or ``None`` when no workspace.

        Public so ``pip-boy doctor`` can render the resolved path even
        when the directory doesn't exist yet — that's a hint for the
        operator that they can ``mkdir`` it themselves."""
        if self._workdir is None:
            return None
        return self._workdir / _THEMES_SUBDIR

    def snapshot(self) -> ThemeDiscovery | None:
        """Return the cached discovery result without re-walking.

        Returns ``None`` when :meth:`discover` has never been called.
        Callers that need a guaranteed-fresh view should call
        :meth:`discover` instead."""
        return self._discovery

    def discover(self) -> ThemeDiscovery:
        """Walk the themes root, build the bundle map, return a snapshot.

        Always re-walks the filesystem — ``/theme refresh`` and
        ``_theme_list`` both rely on this to pick up operator edits
        without a host restart. Call :meth:`snapshot` if you only
        want the cached result from the previous walk.

        The walker tolerates these conditions silently:

        * The ``.pip/themes/`` directory not existing yet (fresh
          workspace). The scaffold creates it on first boot; tests
          that skip the scaffold simply get an empty catalogue.
        * Files (vs. directories) inside the root — ``README.md``
          (written by the scaffold), ``.gitkeep``, etc. Skipped.
        * Hidden directories (``.foo``) and ``__pycache__``. Skipped.

        Validation failures inside ``.pip/themes/`` are caught and
        recorded as :class:`ThemeLoadIssue`; valid themes still
        succeed even if a sibling theme is broken.
        """
        bundles: dict[str, ThemeBundle] = {}
        issues: list[ThemeLoadIssue] = []

        root = self.themes_root
        if root is not None and root.is_dir():
            for entry in self._scan_root(root):
                try:
                    bundle = load_theme_bundle(entry)
                except (
                    ThemeValidationError,
                    OSError,
                    tomllib.TOMLDecodeError,
                ) as exc:
                    log.warning(
                        "Skipping broken theme %s: %s", entry, exc,
                    )
                    issues.append(
                        ThemeLoadIssue(path=entry, reason=str(exc))
                    )
                    continue
                bundles[bundle.manifest.name] = bundle

        snapshot = ThemeDiscovery(
            bundles=bundles,
            issues=tuple(issues),
            count=len(bundles),
        )
        self._discovery = snapshot
        return snapshot

    def get(self, name: str) -> ThemeBundle | None:
        """Return the bundle named ``name`` or ``None`` when missing.

        Lazy: discovers on first access if no snapshot has been built
        yet, so callers can ``ThemeManager(...).get("foo")`` without a
        separate ``discover()`` step in shell-style scripts.
        """
        snapshot = self._discovery or self.discover()
        return snapshot.bundles.get(name)

    def resolve(self, requested: str | None) -> ThemeBundle:
        """Resolve the *active* theme bundle for the host.

        Falls back through ``requested -> DEFAULT_THEME_NAME`` and
        emits a one-line WARNING when ``requested`` does not exist so
        the operator notices a typo without booting blind into a
        silently-different theme. The fallback chain stops at the
        default; if the default itself is missing the function raises
        :class:`LookupError` — which means the scaffold failed to seed
        or the operator deleted every theme, and the caller is expected
        to fall back to whatever it can synthesise.
        """
        snapshot = self._discovery or self.discover()
        if requested:
            bundle = snapshot.bundles.get(requested)
            if bundle is not None:
                return bundle
            log.warning(
                "Requested theme '%s' not found; falling back to '%s'.",
                requested, DEFAULT_THEME_NAME,
            )
        bundle = snapshot.bundles.get(DEFAULT_THEME_NAME)
        if bundle is None:
            raise LookupError(
                f"Default theme '{DEFAULT_THEME_NAME}' missing — "
                f"workspace themes directory is empty or corrupt."
            )
        return bundle

    @staticmethod
    def _scan_root(root: Path) -> list[Path]:
        """Return the immediate-child directories of ``root``, sorted.

        Sorting keeps discovery deterministic across platforms (the
        snapshot tests depend on it). Hidden directories and
        non-directories are filtered — see :meth:`discover` for why.
        """
        if not root.is_dir():
            return []
        out: list[Path] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if child.name == "__pycache__":
                continue
            out.append(child)
        out.sort(key=lambda p: p.name)
        return out
