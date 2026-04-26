"""Tests for the ``/theme`` slash-command family.

These exercise the dispatcher end-to-end (parse → handler → response)
against a real :class:`ThemeManager` rooted at a temporary built-in
directory, plus a real :class:`HostState`. Together they cover:

* ``list`` shows builtin + local origins, marks the active theme,
  and lists broken themes in a "Skipped" section.
* ``show`` reflects the precedence chain (active vs. persisted).
* ``set`` validates the slug, persists to ``host_state.json``, and
  surfaces the "restart to apply" hint when the slug differs from
  what's currently running.
* ``set`` with an unknown slug is rejected with a "Known: ..." list
  and does NOT mutate ``host_state.json``.
* ``/theme`` with no manager (line-mode boot, unit-test contexts)
  short-circuits cleanly — the dispatcher never crashes.

Together these guard the v1 contract: themes are data, slash is a
keyboard shortcut, persistence is workspace-local, no live reload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pip_agent.channels import InboundMessage
from pip_agent.host_commands import CommandContext, dispatch_command
from pip_agent.host_state import HostState
from pip_agent.routing import AgentRegistry, BindingTable
from pip_agent.tui.manager import ThemeManager

# ---------------------------------------------------------------------------
# Fixture helpers
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


def _write_theme(
    root: Path,
    *,
    slug: str,
    display_name: str = "",
    version: str = "0.1.0",
    palette: dict[str, str] | None = None,
) -> Path:
    theme_dir = root / slug
    theme_dir.mkdir(parents=True, exist_ok=True)
    palette_block = "\n".join(
        f'{k} = "{v}"' for k, v in (palette or _VALID_PALETTE).items()
    )
    display_value = display_name or slug.title()
    (theme_dir / "theme.toml").write_text(
        "\n".join(
            [
                "[theme]",
                f'name = "{slug}"',
                f'display_name = "{display_value}"',
                f'version = "{version}"',
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
    return theme_dir


@pytest.fixture
def builtin_root(tmp_path: Path) -> Path:
    root = tmp_path / "builtin"
    root.mkdir()
    _write_theme(root, slug="wasteland", display_name="Wasteland Radiation")
    _write_theme(root, slug="vault-amber", display_name="Vault Amber")
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / ".pip").mkdir(parents=True)
    return ws


def _ctx(
    *,
    workspace: Path,
    text: str,
    theme_manager: ThemeManager | None,
    host_state: HostState | None,
    active_theme_name: str = "wasteland",
) -> CommandContext:
    inbound = InboundMessage(
        text=text, sender_id="cli-user", channel="cli", peer_id="cli-user",
    )
    return CommandContext(
        inbound=inbound,
        registry=AgentRegistry(workspace),
        bindings=BindingTable(),
        bindings_path=workspace / ".pip" / "bindings.json",
        memory_store=None,  # type: ignore[arg-type]
        scheduler=None,
        invalidate_agent=None,
        wechat_controller=None,
        theme_manager=theme_manager,
        host_state=host_state,
        active_theme_name=active_theme_name,
    )


# ---------------------------------------------------------------------------
# Bare /theme + usage
# ---------------------------------------------------------------------------


class TestThemeUsage:
    def test_bare_theme_returns_usage(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme",
                theme_manager=mgr,
                host_state=state,
            )
        )
        assert result.handled is True
        body = result.response or ""
        assert "/theme list" in body
        assert "/theme show" in body
        assert "/theme set" in body

    def test_unknown_subcommand_is_helpful(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme lst",
                theme_manager=mgr,
                host_state=state,
            )
        )
        assert result.handled is True
        body = result.response or ""
        assert "Unknown /theme subcommand 'lst'" in body
        assert "/theme list" in body

    def test_theme_without_manager_short_circuits(
        self, workspace: Path,
    ) -> None:
        # Line-mode boot path: no TUI, no theme manager.
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme list",
                theme_manager=None,
                host_state=None,
                active_theme_name="",
            )
        )
        assert result.handled is True
        body = (result.response or "").lower()
        assert "theme manager is not active" in body


# ---------------------------------------------------------------------------
# /theme list
# ---------------------------------------------------------------------------


class TestThemeList:
    def test_lists_builtin_themes_with_active_marker(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme list",
                theme_manager=mgr,
                host_state=state,
                active_theme_name="wasteland",
            )
        )
        body = result.response or ""
        assert "[builtin] wasteland *" in body
        assert "[builtin] vault-amber" in body
        # The active marker is unique to wasteland, not vault-amber.
        assert "[builtin] vault-amber *" not in body
        assert "Wasteland Radiation" in body
        assert "Vault Amber" in body

    def test_local_theme_overrides_builtin_in_listing(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        # Drop a local theme with the same slug as a builtin — the
        # listing should reflect the *local* origin (because the
        # manager's override took effect). The builtin row must not
        # appear; only the local one wins.
        local_root = workspace / ".pip" / "themes"
        _write_theme(local_root, slug="wasteland", display_name="Local Waste")

        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme list",
                theme_manager=mgr,
                host_state=state,
                active_theme_name="wasteland",
            )
        )
        body = result.response or ""
        assert "[local] wasteland *" in body
        assert "[builtin] wasteland" not in body

    def test_broken_local_theme_is_listed_as_skipped(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        local_root = workspace / ".pip" / "themes"
        broken_dir = local_root / "broken"
        broken_dir.mkdir(parents=True)
        (broken_dir / "theme.toml").write_text(
            "this isn't toml = =\n", encoding="utf-8",
        )
        (broken_dir / "theme.tcss").write_text(
            "Screen { background: black; }\n", encoding="utf-8",
        )

        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme list",
                theme_manager=mgr,
                host_state=state,
            )
        )
        body = result.response or ""
        # Working themes still listed.
        assert "[builtin] wasteland" in body
        # Broken theme surfaced under the Skipped section.
        assert "Skipped" in body
        assert "broken" in body


# ---------------------------------------------------------------------------
# /theme show
# ---------------------------------------------------------------------------


class TestThemeShow:
    def test_shows_active_and_persisted_when_aligned(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        state.set_theme("wasteland")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme show",
                theme_manager=mgr,
                host_state=state,
                active_theme_name="wasteland",
            )
        )
        body = result.response or ""
        assert "Active theme: wasteland" in body
        assert "matches active" in body
        assert "Source: builtin:wasteland" in body

    def test_shows_pending_restart_when_persisted_differs(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        state.set_theme("vault-amber")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme show",
                theme_manager=mgr,
                host_state=state,
                active_theme_name="wasteland",
            )
        )
        body = result.response or ""
        assert "Active theme: wasteland" in body
        assert "Persisted preference: vault-amber" in body
        assert "takes effect after restart" in body

    def test_shows_none_when_no_persisted_preference(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme show",
                theme_manager=mgr,
                host_state=state,
                active_theme_name="wasteland",
            )
        )
        body = result.response or ""
        assert "Persisted preference: (none" in body


# ---------------------------------------------------------------------------
# /theme set
# ---------------------------------------------------------------------------


class TestThemeSet:
    def test_set_persists_known_slug(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme set vault-amber",
                theme_manager=mgr,
                host_state=state,
                active_theme_name="wasteland",
            )
        )
        body = result.response or ""
        assert "Persisted theme 'vault-amber'" in body
        assert "restart pip-boy to apply" in body

        blob = json.loads(state.path.read_text(encoding="utf-8"))
        assert blob == {"tui": {"theme": "vault-amber"}}

    def test_set_to_active_slug_says_already_active(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme set wasteland",
                theme_manager=mgr,
                host_state=state,
                active_theme_name="wasteland",
            )
        )
        body = result.response or ""
        assert "Persisted theme 'wasteland'" in body
        assert "Already active" in body

    def test_set_unknown_slug_lists_options_and_does_not_persist(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme set nope",
                theme_manager=mgr,
                host_state=state,
                active_theme_name="wasteland",
            )
        )
        body = result.response or ""
        assert "Unknown theme 'nope'" in body
        assert "wasteland" in body
        assert "vault-amber" in body
        assert state.path.exists() is False

    def test_set_without_arg_is_usage(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        state = HostState(workspace_pip_dir=workspace / ".pip")
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme set",
                theme_manager=mgr,
                host_state=state,
            )
        )
        assert (result.response or "").startswith("Usage: /theme set <name>")

    def test_set_without_host_state_explains_unavailable(
        self, builtin_root: Path, workspace: Path,
    ) -> None:
        mgr = ThemeManager(builtin_root=builtin_root, workdir=workspace)
        result = dispatch_command(
            _ctx(
                workspace=workspace,
                text="/theme set wasteland",
                theme_manager=mgr,
                host_state=None,
                active_theme_name="wasteland",
            )
        )
        body = (result.response or "").lower()
        assert "host_state is unavailable" in body
