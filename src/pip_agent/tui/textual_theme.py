"""Bridge ``ThemeBundle`` manifest palette → Textual :class:`Theme`.

Built-in ``theme.tcss`` files reference Textual design tokens
(``$surface``, ``$text``, ``$accent``, …). Those resolve from the
**active** Textual theme (default ``textual-dark``), *not* from
``theme.toml`` ``[palette]`` — so without this module the manifest
name (e.g. "Wasteland Radiation") matches the bundle while the screen
still shows Textual's orange accent.

``PipBoyTuiApp`` registers a per-bundle theme named ``pipboy-<slug>``
and sets ``App.theme`` so ``get_css_variables()`` derives ``$*`` from
the authored palette.
"""

from __future__ import annotations

from textual.theme import Theme

from pip_agent.tui.theme_api import ThemeBundle

__all__ = ["textual_theme_from_bundle"]


def textual_theme_from_bundle(bundle: ThemeBundle) -> Theme:
    """Build a :class:`Theme` whose ColorSystem matches ``bundle`` palette."""
    p = bundle.manifest.palette
    slug = bundle.manifest.name
    name = f"pipboy-{slug}"

    # ``text`` / ``text-muted`` default to ``auto …`` in Textual's
    # generator, which can diverge from the hex palette on some
    # terminals. Pin them to the manifest tokens.
    variables: dict[str, str] = {
        "text": p.foreground,
        "text-muted": p.thinking,
        "text-disabled": p.accent_dim,
    }

    return Theme(
        name=name,
        primary=p.accent,
        secondary=p.accent_dim,
        accent=p.accent,
        warning=p.log_warning,
        error=p.log_error,
        success=p.tool_call,
        foreground=p.foreground,
        background=p.background,
        surface=p.background,
        panel=p.status_bar,
        boost=p.status_bar,
        dark=True,
        luminosity_spread=0.12,
        text_alpha=0.95,
        variables=variables,
    )
