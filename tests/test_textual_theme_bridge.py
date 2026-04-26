"""``ThemeBundle`` → Textual ``Theme`` bridge (palette must drive ``$*``)."""

from __future__ import annotations

from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.textual_theme import textual_theme_from_bundle


def test_wasteland_textual_theme_uses_green_accent() -> None:
    bundle = load_builtin_theme("wasteland")
    tt = textual_theme_from_bundle(bundle)
    assert tt.name == "pipboy-wasteland"
    assert tt.primary.lower() == "#7cfc00"
    assert tt.accent.lower() == "#7cfc00"
    assert tt.background.lower() == "#0a0f0a"


def test_vault_amber_textual_theme_uses_amber_accent() -> None:
    bundle = load_builtin_theme("vault-amber")
    tt = textual_theme_from_bundle(bundle)
    assert tt.name == "pipboy-vault-amber"
    assert tt.primary.lower() == "#ffb000"
    assert "text" in tt.variables
    assert tt.variables["text"].lower() == "#ffb000"
