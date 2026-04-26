"""Theme discovery walker (Phase B).

``ThemeManager`` is the single source of truth for which themes are
available at runtime. It walks two roots in a fixed order:

1. **builtin** — :data:`pip_agent.tui.themes.BUILTIN_THEMES_DIR`. Any
   theme that ships with the package. Validation failure is a
   developer bug and surfaces as a hard error during tests; in
   production we still log + skip so a single corrupt builtin can't
   wedge the host.
2. **local** — ``<workspace>/.pip/themes/<name>/``. Operator-installed
   themes. Validation failure here is *expected* (it's user content),
   so we log a WARNING and skip. The walker NEVER raises through to
   the caller for a single broken local theme — that contract is
   exercised by the Phase B test fixtures.

When the two roots define a theme with the same slug, **local wins**.
This lets an operator override a builtin without modifying the package
(the same precedence rule scaffold ``.cursor/`` files use). The
override is logged at INFO so the operator sees a one-shot reminder
when boot announces the active theme.

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
    ThemeBundle,
    ThemeValidationError,
    clamp_art,
    validate_manifest_dict,
)
from pip_agent.tui.themes import BUILTIN_THEMES_DIR

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

Resolved at the bottom of the precedence ladder:
``state.json -> env override -> default``. Defined here (not on the
manager) because the host needs the constant before it constructs the
manager (e.g. when reporting "theme not found" in ``pip-boy doctor``).
"""


_LOCAL_THEMES_SUBDIR = Path(".pip") / "themes"
"""Subpath inside the workspace where operators drop their themes.

A constant rather than a parameter because the path is part of the
user-facing contract documented in ``docs/themes.md``; tests still
override the *workspace* root, not this suffix."""


@dataclass(frozen=True, slots=True)
class ThemeLoadIssue:
    """A single broken theme the manager skipped during discovery.

    Surfaced via :attr:`ThemeManager.issues` so ``pip-boy doctor`` and
    ``/theme list`` can render a one-line "(broken: ...)" hint instead
    of the broken theme silently disappearing.
    """

    origin: str
    """``"builtin"`` or ``"local"`` — same vocabulary as
    :attr:`ThemeBundle.source`."""

    path: Path
    """Theme directory the loader rejected (not the manifest path),
    so the user can ``ls`` it directly."""

    reason: str
    """Human-readable explanation; trimmed for log noise."""


def load_theme_bundle(theme_dir: Path, *, origin: str) -> ThemeBundle:
    """Load and validate one theme directory into a :class:`ThemeBundle`.

    ``origin`` is recorded into :attr:`ThemeBundle.source` so the same
    bundle is self-describing in ``/theme list`` output.

    The function performs the *full* schema check (including the
    "manifest name matches directory name" invariant from the loader
    contract). Failures raise :class:`ThemeValidationError` (schema)
    or surface the underlying :class:`OSError` / :class:`tomllib.TOMLDecodeError`.
    The discovery walker catches these so a single bad theme doesn't
    take down boot — but a *direct* caller (e.g. the Phase A
    bootstrap path used by ``runner.build_app``) still gets the raw
    exception, which is the desired behaviour for builtins where a
    broken file is a developer bug.
    """
    if not theme_dir.is_dir():
        raise FileNotFoundError(f"Theme directory not found: {theme_dir}")

    name = theme_dir.name
    manifest_path = theme_dir / "theme.toml"
    tcss_path = theme_dir / "theme.tcss"
    art_path = theme_dir / "art.txt"

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

    art_text = ""
    art_truncated = False
    if manifest.show_art and art_path.exists():
        raw_art = art_path.read_text(encoding="utf-8")
        art_text, art_truncated = clamp_art(raw_art)
        if art_truncated:
            log.warning(
                "Theme '%s' (%s) art exceeds %dx%d limit; truncated.",
                name, origin, 32, 8,
            )

    return ThemeBundle(
        manifest=manifest,
        tcss=tcss,
        art=art_text,
        source=f"{origin}:{name}",
        art_truncated=art_truncated,
    )


@dataclass(frozen=True, slots=True)
class ThemeDiscovery:
    """Result of one :meth:`ThemeManager.discover` pass.

    Returned (and cached) by the manager so tests can assert against an
    immutable snapshot rather than peeking at private state.
    """

    bundles: dict[str, ThemeBundle]
    """Slug → bundle, with local overrides applied."""

    issues: tuple[ThemeLoadIssue, ...]
    """All themes that failed validation, in scan order."""

    builtin_count: int
    """How many *valid* builtin themes were found. Doctor surfaces
    this so an operator sees "0 builtins discovered" as a smoke
    signal that the package install is broken."""

    local_count: int
    """How many *valid* local themes were found."""


class ThemeManager:
    """Discover + look up themes from builtin and workspace roots.

    The manager is constructed once per host boot; ``discover()`` is
    called eagerly so by the time the TUI builds its App, the active
    theme is already validated. Subsequent calls re-walk the
    filesystem — useful for tests, but the host doesn't expose a
    re-scan command in v1 because live theme reload is out of scope
    (design.md §"不在 v1 范围").

    ``builtin_root`` is overridable so a unit test can construct an
    isolated themes tree without monkey-patching the module-level
    constant. ``workdir`` is similarly overridable; pass ``None`` to
    skip the local scan entirely (used by the very early ``doctor``
    bootstrap before ``WORKDIR`` is resolved).
    """

    def __init__(
        self,
        *,
        builtin_root: Path | None = None,
        workdir: Path | None = None,
    ) -> None:
        self._builtin_root = builtin_root or BUILTIN_THEMES_DIR
        self._workdir = workdir
        self._discovery: ThemeDiscovery | None = None

    @property
    def local_root(self) -> Path | None:
        """Where local themes live, or ``None`` when no workspace.

        Public so ``pip-boy doctor`` can render the resolved path even
        when the directory doesn't exist yet — that's a hint for the
        operator that they can ``mkdir`` it themselves."""
        if self._workdir is None:
            return None
        return self._workdir / _LOCAL_THEMES_SUBDIR

    def discover(self) -> ThemeDiscovery:
        """Walk both roots, build the bundle map, return a snapshot.

        Walk order: builtin first, then local. Local entries replace
        builtin entries with the same slug (and the override fact is
        logged at INFO, once per discovery, so the operator sees it).

        The walker tolerates these conditions silently:

        * The ``.pip/themes/`` directory not existing yet (fresh
          workspace). Operators ``mkdir`` it on their own time.
        * Files (vs. directories) inside either root — likely
          ``README.md`` / ``.gitkeep``. They are skipped.
        * Hidden directories (``.foo``) — also skipped, so dotfiles
          like the scaffold's ``.gitkeep`` placeholder don't get
          interpreted as a malformed theme.

        Validation failures inside ``.pip/themes/`` are caught and
        recorded as :class:`ThemeLoadIssue`; ``ok=True`` themes still
        succeed even if a sibling theme is broken.
        """
        bundles: dict[str, ThemeBundle] = {}
        issues: list[ThemeLoadIssue] = []
        builtin_count = 0
        local_count = 0

        for entry in self._scan_root(self._builtin_root):
            try:
                bundle = load_theme_bundle(entry, origin="builtin")
            except (
                ThemeValidationError,
                OSError,
                tomllib.TOMLDecodeError,
            ) as exc:
                log.warning(
                    "Skipping broken builtin theme %s: %s",
                    entry, exc,
                )
                issues.append(
                    ThemeLoadIssue(
                        origin="builtin",
                        path=entry,
                        reason=str(exc),
                    )
                )
                continue
            bundles[bundle.manifest.name] = bundle
            builtin_count += 1

        local_root = self.local_root
        if local_root is not None and local_root.is_dir():
            for entry in self._scan_root(local_root):
                try:
                    bundle = load_theme_bundle(entry, origin="local")
                except (
                    ThemeValidationError,
                    OSError,
                    tomllib.TOMLDecodeError,
                ) as exc:
                    log.warning(
                        "Skipping broken local theme %s: %s",
                        entry, exc,
                    )
                    issues.append(
                        ThemeLoadIssue(
                            origin="local",
                            path=entry,
                            reason=str(exc),
                        )
                    )
                    continue
                if bundle.manifest.name in bundles:
                    log.info(
                        "Local theme '%s' overrides builtin of the same name.",
                        bundle.manifest.name,
                    )
                bundles[bundle.manifest.name] = bundle
                local_count += 1

        snapshot = ThemeDiscovery(
            bundles=bundles,
            issues=tuple(issues),
            builtin_count=builtin_count,
            local_count=local_count,
        )
        self._discovery = snapshot
        return snapshot

    def get(self, name: str) -> ThemeBundle | None:
        """Return the bundle named ``name`` or ``None`` when missing.

        Lazy: discovers on first access if no snapshot has been built
        yet, so callers can ``ThemeManager(...).get("foo")`` without
        a separate ``discover()`` step in shell-style scripts.
        """
        snapshot = self._discovery or self.discover()
        return snapshot.bundles.get(name)

    def resolve(self, requested: str | None) -> ThemeBundle:
        """Resolve the *active* theme bundle for the host.

        Falls back through ``requested -> DEFAULT_THEME_NAME`` and
        emits a one-line WARNING when ``requested`` does not exist
        (so the operator notices a typo without booting blind into a
        silently-different theme). The fallback chain stops at the
        default; if the default itself is missing the function raises
        :class:`LookupError`, which is a hard developer bug because
        the default ships with the package.
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
                f"Default theme '{DEFAULT_THEME_NAME}' missing — package install is broken."
            )
        return bundle

    @staticmethod
    def _scan_root(root: Path) -> list[Path]:
        """Return the immediate-child directories of ``root``, sorted.

        Sorting keeps discovery deterministic across platforms (the
        snapshot tests in Phase B.4 depend on it). Hidden directories
        and non-directories are filtered out — see :meth:`discover`
        for why.
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
