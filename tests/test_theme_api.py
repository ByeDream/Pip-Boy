"""Theme manifest validation contract tests.

These pin the v1 theme schema so a Phase B / Phase C extension never
silently drifts the surface theme authors program against.
"""

from __future__ import annotations

import pytest

from pip_agent.tui.theme_api import (
    ART_MAX_COLUMNS,
    ART_MAX_ROWS,
    PALETTE_TOKENS,
    ThemeBundle,
    ThemeManifest,
    ThemePalette,
    ThemeValidationError,
    clamp_art,
    validate_manifest_dict,
    validate_palette_dict,
)


# ---------------------------------------------------------------------------
# Palette tokens
# ---------------------------------------------------------------------------


def _full_palette() -> dict[str, str]:
    return {
        "background": "#000000",
        "foreground": "#cccccc",
        "accent": "#7CFC00",
        "accent_dim": "#3a8000",
        "user_input": "#aaffaa",
        "agent_text": "#7CFC00",
        "thinking": "#666666",
        "tool_call": "#88ddff",
        "log_info": "#888888",
        "log_warning": "#ffcc66",
        "log_error": "#ff6666",
        "status_bar": "#1a1a1a",
        "status_bar_text": "#7CFC00",
    }


class TestPaletteTokens:
    def test_locked_token_set(self) -> None:
        # Adding/removing tokens is a breaking change for theme authors
        # — assert the explicit list here so any drift fails CI.
        assert set(PALETTE_TOKENS) == {
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
        }

    def test_full_palette_validates(self) -> None:
        palette = validate_palette_dict(_full_palette())
        assert isinstance(palette, ThemePalette)
        assert palette.accent == "#7CFC00"

    def test_missing_token_rejected(self) -> None:
        broken = _full_palette()
        del broken["accent"]
        with pytest.raises(ThemeValidationError, match="missing palette tokens: accent"):
            validate_palette_dict(broken)

    def test_empty_value_rejected(self) -> None:
        broken = _full_palette()
        broken["accent"] = ""
        with pytest.raises(ThemeValidationError, match="non-empty"):
            validate_palette_dict(broken)


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------


def _full_manifest_dict() -> dict[str, object]:
    return {
        "theme": {
            "name": "wasteland",
            "display_name": "Wasteland Radiation",
            "version": "0.1.0",
            "author": "Pip-Boy",
            "description": "Default Pip-Boy green-on-black wasteland theme.",
        },
        "palette": _full_palette(),
    }


class TestManifestValidation:
    def test_minimal_full_manifest(self) -> None:
        manifest = validate_manifest_dict(_full_manifest_dict())
        assert isinstance(manifest, ThemeManifest)
        assert manifest.name == "wasteland"
        assert manifest.show_art is True
        assert manifest.show_app_log is True
        assert manifest.show_status_bar is True

    def test_widget_toggles_can_be_overridden(self) -> None:
        data = _full_manifest_dict()
        data["theme"]["show_art"] = False  # type: ignore[index]
        data["theme"]["show_app_log"] = False  # type: ignore[index]
        manifest = validate_manifest_dict(data)
        assert manifest.show_art is False
        assert manifest.show_app_log is False

    def test_footer_template_overridable(self) -> None:
        data = _full_manifest_dict()
        data["theme"]["footer_template"] = "[{turns}t / {cost}]"  # type: ignore[index]
        manifest = validate_manifest_dict(data)
        assert manifest.footer_template == "[{turns}t / {cost}]"

    @pytest.mark.parametrize(
        "bad_name", ["", "Wasteland", "waste land", "1wasteland", "waste_land"],
    )
    def test_name_must_be_slug(self, bad_name: str) -> None:
        data = _full_manifest_dict()
        data["theme"]["name"] = bad_name  # type: ignore[index]
        with pytest.raises(ThemeValidationError):
            validate_manifest_dict(data)

    def test_missing_theme_section_rejected(self) -> None:
        with pytest.raises(ThemeValidationError, match="missing \\[theme\\]"):
            validate_manifest_dict({"palette": _full_palette()})

    def test_missing_palette_section_rejected(self) -> None:
        with pytest.raises(
            ThemeValidationError, match="missing \\[palette\\]"
        ):
            validate_manifest_dict({"theme": _full_manifest_dict()["theme"]})

    def test_non_bool_widget_toggle_rejected(self) -> None:
        data = _full_manifest_dict()
        data["theme"]["show_art"] = "yes"  # type: ignore[index]
        with pytest.raises(ThemeValidationError, match="must be a boolean"):
            validate_manifest_dict(data)


# ---------------------------------------------------------------------------
# Art clamp
# ---------------------------------------------------------------------------


class TestClampArt:
    def test_within_limits_unchanged(self) -> None:
        art = "abc\n" * 4
        clamped, truncated = clamp_art(art)
        assert clamped == art.rstrip("\n")
        assert truncated is False

    def test_too_many_rows_dropped(self) -> None:
        art = "x\n" * (ART_MAX_ROWS + 5)
        clamped, truncated = clamp_art(art)
        assert clamped.count("\n") == ART_MAX_ROWS - 1
        assert truncated is True

    def test_too_wide_lines_truncated(self) -> None:
        art = "y" * (ART_MAX_COLUMNS + 10)
        clamped, truncated = clamp_art(art)
        assert len(clamped) == ART_MAX_COLUMNS
        assert truncated is True


# ---------------------------------------------------------------------------
# Bundle data class
# ---------------------------------------------------------------------------


class TestThemeBundle:
    def test_bundle_carries_manifest_tcss_art_source(self) -> None:
        manifest = validate_manifest_dict(_full_manifest_dict())
        bundle = ThemeBundle(
            manifest=manifest,
            tcss="Screen { background: #000; }",
            art="* * *",
            source="builtin:wasteland",
        )
        assert bundle.manifest.name == "wasteland"
        assert "background" in bundle.tcss
        assert bundle.source == "builtin:wasteland"
        assert bundle.art_truncated is False

    def test_bundle_is_frozen(self) -> None:
        manifest = validate_manifest_dict(_full_manifest_dict())
        bundle = ThemeBundle(
            manifest=manifest, tcss="", art="", source="builtin:x",
        )
        with pytest.raises((AttributeError, TypeError)):
            bundle.tcss = "mutated"  # type: ignore[misc]
