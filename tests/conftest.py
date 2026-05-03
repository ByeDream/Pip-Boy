"""Shared pytest fixtures for the Pip-Boy test suite."""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _disable_lazy_plugin_bootstrap():
    """Keep ``pip_agent.plugins.ensure_bootstrap_once`` a no-op during tests.

    The module gates every public coroutine (``marketplace_*``,
    ``plugin_*``) on a one-shot marketplace bootstrap driven by
    ``settings.bootstrap_marketplaces``. Tests stub the lower-level
    ``plug._run`` subprocess seam, so a live gate would consume an
    extra ``marketplace list`` result from the test queue and corrupt
    argv assertions.

    Pre-marking ``_bootstrap_done`` at setup + resetting at teardown
    keeps tests hermetic without every file re-declaring the dance.
    Tests that need to exercise the gate itself call
    :func:`pip_agent.plugins.reset_bootstrap_for_test` explicitly.
    """
    from pip_agent import plugins as _plug

    _plug.reset_bootstrap_for_test()
    _plug._bootstrap_done = True
    try:
        yield
    finally:
        _plug.reset_bootstrap_for_test()


@pytest.fixture(autouse=True)
def _pin_backend_to_claude_code():
    """Default ``settings.backend`` to ``claude_code`` for all tests.

    The live ``.env`` may set ``BACKEND=codex_cli`` for manual testing.
    Without this pin, hundreds of existing tests that implicitly assume
    the claude_code backend would break. Tests that need a different
    backend should mock ``settings.backend`` explicitly.
    """
    with patch("pip_agent.config.settings.backend", "claude_code"):
        yield
