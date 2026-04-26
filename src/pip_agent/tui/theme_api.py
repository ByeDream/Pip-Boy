"""Theme contract: locked v1 schema for ``theme.toml`` + bundle loading.

Themes are pure data. A theme is a directory containing:

* ``theme.toml``  — manifest (name, version, palette, widget toggles,
  footer template). Schema is locked by :class:`ThemeManifest` /
  :class:`ThemePalette`.
* ``theme.tcss``  — Textual CSS, free-form; the :class:`PipBoyTuiApp`
  loads it as a stylesheet.
* ``art.txt`` (optional) — ASCII art rendered into the ``#pipboy-art``
  widget. Hard-capped at 32 columns × 8 rows to honour the
  "工作软件不是玩具" budget from design.md §9. Files exceeding the cap
  are accepted with a warning and trimmed at load time so a buggy
  theme cannot blow out a narrow terminal layout.

What themes can NOT do (LOCKED):

* Change widget topology — sinks and panes are framework-owned.
* Run Python code — there is no entry-point hook in v1.
* Listen on inbound channels, call ``app.exit()``, write stdout, or
  configure logging.
* Add new palette tokens — extending the schema is a host-version
  change, not a theme change.

Phase A ships the manifest/bundle dataclasses and the validation
helper. Phase B adds the discovery walker (``ThemeManager.discover``)
that scans builtin + ``<workspace>/.pip/themes/`` and produces a list
of :class:`ThemeBundle` objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ART_MAX_COLUMNS",
    "ART_MAX_ROWS",
    "BANNER_MAX_COLUMNS",
    "BANNER_MAX_ROWS",
    "DECO_MAX_COLUMNS",
    "DECO_MAX_ROWS",
    "PALETTE_TOKENS",
    "ThemeBundle",
    "ThemeManifest",
    "ThemePalette",
    "ThemeValidationError",
    "clamp_banner",
    "clamp_deco",
    "validate_palette_dict",
]

# ---------------------------------------------------------------------------
# Hard limits
# ---------------------------------------------------------------------------

ART_MAX_COLUMNS: int = 32
"""Maximum columns of legacy ``art.txt`` content.

Kept for v1 theme compatibility: themes that pre-date the banner/deco
split still ship a single ``art.txt`` and the loader falls it back
into the banner slot. New themes should use ``banner.txt`` +
``deco.txt`` instead."""

ART_MAX_ROWS: int = 8
"""Maximum rows of legacy ``art.txt`` content."""

BANNER_MAX_COLUMNS: int = 32
"""Maximum columns for the theme's top banner (``banner.txt``).

The side pane is fixed at 34 columns with 1-col horizontal padding,
giving 32 usable columns. Banners that overflow are truncated at load
time — a warning surfaces via ``pip-boy doctor`` and the logger."""

BANNER_MAX_ROWS: int = 6
"""Maximum rows for the theme's top banner (``banner.txt``)."""

DECO_MAX_COLUMNS: int = 32
"""Maximum columns for the theme's decoration (``deco.txt``).

The decoration sits below the banner in the top of ``#side-pane``
(Vault-Boy head for wasteland, retro-sci-fi motif for vault-amber).
Same 32-column usable budget as the banner."""

DECO_MAX_ROWS: int = 6
"""Maximum rows for the theme's decoration (``deco.txt``)."""


# Slug rule for ``ThemeManifest.name``: lowercase letters, digits, dash.
# Single source of truth used both at validation time and by the loader
# in Phase B when matching directory names against manifest names.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


# ---------------------------------------------------------------------------
# Palette schema (LOCKED)
# ---------------------------------------------------------------------------

# Token names theme authors must provide. Adding a token here is a
# breaking change for every existing theme. Removing one is too. Phase
# C documents these in ``docs/themes.md``; the doc is generated from
# this list to keep them in sync.
PALETTE_TOKENS: tuple[str, ...] = (
    "background",
    "foreground",
    "accent",
    "accent_dim",
    "user_input",
    "agent_text",
    "thinking",
    "tool_call",
    "log_info",
    "log_warning",
    "log_error",
    "status_bar",
    "status_bar_text",
)


@dataclass(frozen=True, slots=True)
class ThemePalette:
    """Color tokens consumed by Pip-Boy widgets.

    Values are strings — Textual accepts hex (``"#7CFC00"``), named
    colors (``"green"``), and ``rgb(...)`` notation. At runtime
    :func:`pip_agent.tui.textual_theme.textual_theme_from_bundle` maps
    this table onto a registered Textual :class:`textual.theme.Theme`
    so ``$surface`` / ``$accent`` / … in ``theme.tcss`` resolve from
    the manifest instead of the global ``textual-dark`` defaults.
    """

    background: str
    foreground: str
    accent: str
    accent_dim: str
    user_input: str
    agent_text: str
    thinking: str
    tool_call: str
    log_info: str
    log_warning: str
    log_error: str
    status_bar: str
    status_bar_text: str


# ---------------------------------------------------------------------------
# Manifest schema (LOCKED)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThemeManifest:
    """``theme.toml`` schema, v1.

    ``name`` is the slug used by ``/theme set <name>`` and by directory
    matching during Phase B discovery. ``display_name`` is what shows
    in ``/theme list`` output.

    ``footer_template`` is rendered with str.format()-style fields:
    ``{turns}``, ``{cost}``, ``{elapsed_s}``, ``{tokens_in}``,
    ``{tokens_out}``, ``{tools}``. Themes may use any subset; missing
    fields render as empty strings rather than raising, so future
    additions don't break older themes.
    """

    name: str
    display_name: str
    version: str
    author: str
    description: str
    palette: ThemePalette
    show_art: bool = True
    show_app_log: bool = True
    show_status_bar: bool = True
    footer_template: str = (
        "[{tools} tools - {turns} turns - {elapsed_s}s - ${cost}]"
    )


# ---------------------------------------------------------------------------
# Bundle (in-memory representation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThemeBundle:
    """Loaded theme: manifest + raw TCSS + clamped art + on-disk path.

    ``path`` records the directory the bundle was loaded from so
    ``/theme list``, ``pip-boy doctor`` and the TUI's live-apply logic
    can point the operator at the exact place to edit.
    """

    manifest: ThemeManifest
    tcss: str
    art: str
    """Legacy single-block ASCII art from ``art.txt``. Populated for
    pre-split themes; the loader uses it as a banner fallback when a
    theme has no ``banner.txt``. New themes should leave ``art.txt``
    empty and supply ``banner.txt`` + ``deco.txt`` separately."""

    banner: str = ""
    """Clamped ``banner.txt`` — the big title block rendered in the top
    of ``#side-pane``. Empty string when the theme supplies neither
    ``banner.txt`` nor ``art.txt``."""

    deco: str = ""
    """Clamped ``deco.txt`` — the small decoration below the banner
    (Vault-Boy head, retro-sci-fi motif, etc). Empty when the theme
    doesn't ship one."""

    path: Path = field(default_factory=Path)
    """Directory the theme was loaded from, e.g.
    ``<workspace>/.pip/themes/wasteland``. Always an absolute path
    after :func:`pip_agent.tui.manager.load_theme_bundle` returns."""

    art_truncated: bool = False
    """True iff the loader trimmed ``art`` to fit ``ART_MAX_*`` limits.
    Surfaced via ``pip-boy doctor`` so the operator sees the warning
    once instead of having to scrape boot logs."""

    banner_truncated: bool = False
    """True iff the loader trimmed ``banner`` to fit ``BANNER_MAX_*`` limits."""

    deco_truncated: bool = False
    """True iff the loader trimmed ``deco`` to fit ``DECO_MAX_*`` limits."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ThemeValidationError(ValueError):
    """Raised when a theme manifest fails the v1 schema check.

    The loader (Phase B) catches this, logs a WARNING, and skips the
    offending theme — a single broken file in ``.pip/themes/`` must
    never wedge host boot.
    """


def _require_string(data: dict[str, Any], key: str, *, where: str) -> str:
    val = data.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ThemeValidationError(
            f"{where}: missing or empty string field '{key}'"
        )
    return val


def _require_bool(
    data: dict[str, Any], key: str, default: bool, *, where: str
) -> bool:
    if key not in data:
        return default
    val = data[key]
    if not isinstance(val, bool):
        raise ThemeValidationError(
            f"{where}: field '{key}' must be a boolean, got {type(val).__name__}"
        )
    return val


def validate_palette_dict(
    data: dict[str, Any], *, where: str = "theme.toml [palette]"
) -> ThemePalette:
    """Validate a raw palette dict and return a :class:`ThemePalette`.

    All :data:`PALETTE_TOKENS` must be present and non-empty strings.
    Extra keys are tolerated with no warning — that lets future Pip-Boy
    versions add palette tokens without breaking older themes (the
    older theme just won't supply the new token, which Phase C's
    loader fills with the builtin default).
    """
    missing = [k for k in PALETTE_TOKENS if k not in data]
    if missing:
        raise ThemeValidationError(
            f"{where}: missing palette tokens: {', '.join(sorted(missing))}"
        )
    kwargs: dict[str, str] = {}
    for token in PALETTE_TOKENS:
        val = data[token]
        if not isinstance(val, str) or not val.strip():
            raise ThemeValidationError(
                f"{where}: palette token '{token}' must be a non-empty string"
            )
        kwargs[token] = val
    return ThemePalette(**kwargs)


def validate_manifest_dict(
    data: dict[str, Any], *, where: str = "theme.toml"
) -> ThemeManifest:
    """Validate a parsed ``theme.toml`` dict and return a manifest.

    Schema:

    * ``[theme]`` section:
        - ``name`` (slug, required)
        - ``display_name`` (required)
        - ``version`` (required)
        - ``author`` (required)
        - ``description`` (required)
        - ``show_art`` (optional bool, default True)
        - ``show_app_log`` (optional bool, default True)
        - ``show_status_bar`` (optional bool, default True)
        - ``footer_template`` (optional string)
    * ``[palette]`` section: see :func:`validate_palette_dict`.
    """
    theme = data.get("theme")
    if not isinstance(theme, dict):
        raise ThemeValidationError(f"{where}: missing [theme] section")
    palette = data.get("palette")
    if not isinstance(palette, dict):
        raise ThemeValidationError(f"{where}: missing [palette] section")

    name = _require_string(theme, "name", where=f"{where} [theme]")
    if not _NAME_RE.match(name):
        raise ThemeValidationError(
            f"{where} [theme]: name '{name}' must match {_NAME_RE.pattern}"
        )
    display_name = _require_string(
        theme, "display_name", where=f"{where} [theme]"
    )
    version = _require_string(theme, "version", where=f"{where} [theme]")
    author = _require_string(theme, "author", where=f"{where} [theme]")
    description = _require_string(
        theme, "description", where=f"{where} [theme]"
    )
    show_art = _require_bool(
        theme, "show_art", True, where=f"{where} [theme]"
    )
    show_app_log = _require_bool(
        theme, "show_app_log", True, where=f"{where} [theme]"
    )
    show_status_bar = _require_bool(
        theme, "show_status_bar", True, where=f"{where} [theme]"
    )
    footer_template = theme.get(
        "footer_template",
        "[{tools} tools - {turns} turns - {elapsed_s}s - ${cost}]",
    )
    if not isinstance(footer_template, str):
        raise ThemeValidationError(
            f"{where} [theme]: 'footer_template' must be a string"
        )

    palette_obj = validate_palette_dict(
        palette, where=f"{where} [palette]"
    )

    return ThemeManifest(
        name=name,
        display_name=display_name,
        version=version,
        author=author,
        description=description,
        palette=palette_obj,
        show_art=show_art,
        show_app_log=show_app_log,
        show_status_bar=show_status_bar,
        footer_template=footer_template,
    )


def _clamp_block(
    text: str, *, max_rows: int, max_cols: int,
) -> tuple[str, bool]:
    """Clamp a block of ASCII text to ``(max_rows, max_cols)``.

    Returns the clamped text plus a boolean indicating whether
    truncation actually happened. Lines longer than ``max_cols`` are
    truncated; rows beyond ``max_rows`` are dropped. Shared between
    :func:`clamp_art`, :func:`clamp_banner`, :func:`clamp_deco`.
    """
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_rows:
        lines = lines[:max_rows]
        truncated = True
    clamped: list[str] = []
    for ln in lines:
        if len(ln) > max_cols:
            clamped.append(ln[:max_cols])
            truncated = True
        else:
            clamped.append(ln)
    return "\n".join(clamped), truncated


def clamp_art(text: str) -> tuple[str, bool]:
    """Clamp legacy ``art.txt`` text to (:data:`ART_MAX_ROWS`,
    :data:`ART_MAX_COLUMNS`)."""
    return _clamp_block(text, max_rows=ART_MAX_ROWS, max_cols=ART_MAX_COLUMNS)


def clamp_banner(text: str) -> tuple[str, bool]:
    """Clamp ``banner.txt`` text to (:data:`BANNER_MAX_ROWS`,
    :data:`BANNER_MAX_COLUMNS`)."""
    return _clamp_block(
        text, max_rows=BANNER_MAX_ROWS, max_cols=BANNER_MAX_COLUMNS,
    )


def clamp_deco(text: str) -> tuple[str, bool]:
    """Clamp ``deco.txt`` text to (:data:`DECO_MAX_ROWS`,
    :data:`DECO_MAX_COLUMNS`)."""
    return _clamp_block(
        text, max_rows=DECO_MAX_ROWS, max_cols=DECO_MAX_COLUMNS,
    )


# ---------------------------------------------------------------------------
# Phase B will add a ``ThemeManager.discover()`` walker on top of the
# helpers above. Callers in Phase A interact only with
# :func:`validate_manifest_dict`, :class:`ThemeBundle`, and
# :func:`clamp_art` — that surface is the locked v1 contract.
# ---------------------------------------------------------------------------
