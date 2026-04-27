"""Logging-configuration tripwire for ``pip_agent.__main__``.

Pip-Boy uses stdlib ``logging`` for scheduler ticks, heartbeat sentinel
suppression, session ids, SDK cost/turn summaries, MCP tool calls, and the
reflect pipeline. Python's default root-logger threshold is WARNING, which
means every ``log.info`` / ``log.debug`` emitted by pip_agent is silently
dropped unless ``logging.basicConfig`` runs first.

Previous refactors repeatedly lost that wiring — the host came up, the
scheduler fired heartbeats, the agent replied HEARTBEAT_OK, dispatch
silenced it, and the CLI showed **nothing** because every internal event was
below WARNING. To an operator, the host looked frozen.

These tests lock the contract down:

* ``_configure_logging`` honours ``VERBOSE`` as a DEBUG toggle for
  Pip-Boy's own code only: ``pip_agent.*`` at DEBUG when VERBOSE=true
  and INFO otherwise. Third parties ride the root level — INFO under
  VERBOSE=true, WARNING otherwise — so flipping VERBOSE never changes
  what third parties emit at INFO/DEBUG.
* ``main()`` always calls ``_configure_logging`` *before* handing control
  to ``run_host``. If a future change drops that call site the regression
  test fails loudly.
"""
from __future__ import annotations

import logging

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_root_logger():
    """Snapshot + restore root / ``pip_agent`` logger state per test.

    ``logging.basicConfig`` is a no-op after the first successful call in a
    process. Tests that want to observe fresh configuration therefore need
    to clear existing handlers (and restore them afterwards so pytest's own
    logging isn't broken).

    We also snapshot the ``pip_agent`` logger's level because the
    verbose-path code mutates it explicitly; without a restore, whichever
    test runs last would leak its level into the rest of the suite.
    """
    root = logging.getLogger()
    pip_logger = logging.getLogger("pip_agent")

    saved_root_handlers = root.handlers[:]
    saved_root_level = root.level
    saved_pip_level = pip_logger.level

    root.handlers.clear()
    yield root
    root.handlers[:] = saved_root_handlers
    root.setLevel(saved_root_level)
    pip_logger.setLevel(saved_pip_level)


# ---------------------------------------------------------------------------
# _configure_logging contract
# ---------------------------------------------------------------------------


class TestConfigureLoggingHonoursVerbose:
    """Two-tier layout: root level tracks VERBOSE, pip_agent gets a bump."""

    def test_verbose_true_sets_root_to_info(self, fresh_root_logger, monkeypatch):
        # Root at INFO (not DEBUG) under verbose — third parties ride the
        # root level and we specifically do NOT want their DEBUG firehose.
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", True)
        main_mod._configure_logging()

        assert fresh_root_logger.level == logging.INFO

    def test_verbose_true_bumps_pip_agent_to_debug(
        self, fresh_root_logger, monkeypatch,
    ):
        # pip_agent is the ONE logger that should see DEBUG under verbose
        # — that is the whole point of the firehose switch.
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", True)
        main_mod._configure_logging()

        assert logging.getLogger("pip_agent").level == logging.DEBUG

    def test_verbose_false_sets_root_to_warning(
        self, fresh_root_logger, monkeypatch,
    ):
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", False)
        main_mod._configure_logging()

        assert fresh_root_logger.level == logging.WARNING

    def test_verbose_false_pins_pip_agent_to_info(
        self, fresh_root_logger, monkeypatch,
    ):
        # Quiet mode keeps pip_agent at INFO — startup, channel bring-up
        # and scheduler context still surface through stdout, the TUI
        # app-log pane, and pip-boy.log. Only DEBUG is suppressed. If a
        # future change pushes pip_agent back to NOTSET/WARNING under
        # quiet mode, the TUI's bottom-right pane goes empty during
        # healthy runs — the operator regression this test guards.
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", False)
        main_mod._configure_logging()

        assert logging.getLogger("pip_agent").level == logging.INFO


class TestThirdPartyLibsRideRootLevel:
    """Non-HTTP third parties are NOT pinned — they inherit root.

    The original design rule was "only pip_agent needs a pin, everyone
    else rides root". That still holds for SDK / MCP / asyncio — their
    INFO output is useful startup, session, and scheduler context.

    The exception is HTTP wire-chatter (``httpx`` / ``httpcore`` /
    ``urllib3`` / ``h11``) — see ``TestHttpWireChatterIsCapped`` for the
    operational reason those libs are hard-pinned at WARNING.
    """

    @pytest.mark.parametrize(
        "third_party",
        ["mcp", "claude_agent_sdk", "asyncio", "anyio"],
    )
    def test_third_party_is_not_explicitly_pinned(
        self, fresh_root_logger, monkeypatch, third_party,
    ):
        # ``NOTSET`` (0) = "inherit from ancestor (root)". If someone re-adds
        # an explicit ``setLevel`` for one of these, this fires.
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", True)
        main_mod._configure_logging()

        level = logging.getLogger(third_party).level
        assert level == logging.NOTSET, (
            f"{third_party!r} was pinned (level={level}); non-HTTP third "
            "parties should ride the root level so VERBOSE=false silences "
            "them and VERBOSE=true shows their INFO (not DEBUG)"
        )


class TestHttpWireChatterIsCapped:
    """HTTP-layer libs are hard-pinned at WARNING regardless of VERBOSE.

    The operational bug that drove this: the WeChat iLink ``getupdates``
    endpoint fast-returns (no server-side long-poll hold) when there's
    nothing to deliver, at roughly 20 req/sec. With root at INFO under
    VERBOSE=true, ``httpx`` emits one line per request —
    ``HTTP Request: POST .../getupdates "HTTP/1.1 200 OK"`` — every 50ms.
    The CLI prompt scrolls off-screen continuously and the user can't
    type. INFO from httpx / httpcore / urllib3 / h11 carries ~zero signal
    for Pip-Boy (errors come through at WARNING+ anyway), so the fix is
    to cap them at WARNING unconditionally.

    If these start emitting useful INFO that we want back, revisit —
    but do NOT just remove the pin, because the CLI unusability will
    come straight back.
    """

    @pytest.mark.parametrize(
        "http_lib", ["httpx", "httpcore", "urllib3", "h11"],
    )
    def test_http_lib_is_capped_under_verbose(
        self, fresh_root_logger, monkeypatch, http_lib,
    ):
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", True)
        main_mod._configure_logging()

        assert logging.getLogger(http_lib).level == logging.WARNING, (
            f"{http_lib!r} must be pinned at WARNING even under VERBOSE — "
            "otherwise WeChat long-poll floods the CLI stdout"
        )

    @pytest.mark.parametrize(
        "http_lib", ["httpx", "httpcore", "urllib3", "h11"],
    )
    def test_http_lib_is_capped_under_quiet(
        self, fresh_root_logger, monkeypatch, http_lib,
    ):
        # Quiet mode already suppresses INFO at root, but keep the pin
        # explicit so flipping VERBOSE doesn't change per-lib behaviour.
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", False)
        main_mod._configure_logging()

        assert logging.getLogger(http_lib).level == logging.WARNING


class TestConfigureLoggingActuallyEmits:
    """End-to-end smoke: configure → emit → verify it lands on stdout.

    The level/handler checks above can all pass while the logger is still
    silent (e.g. if we accidentally stopped passing ``stream=sys.stdout``).
    These tests exercise the wire.
    """

    def test_pip_agent_debug_reaches_stdout_when_verbose(
        self, fresh_root_logger, monkeypatch, capsys,
    ):
        # The whole point of VERBOSE=true: pip_agent DEBUG lands on stdout.
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", True)
        main_mod._configure_logging()

        logging.getLogger("pip_agent.host_scheduler").debug("tick debug")
        logging.getLogger("pip_agent.host_scheduler").info("tick info")

        captured = capsys.readouterr()
        assert "tick debug" in captured.out
        assert "tick info" in captured.out

    def test_third_party_debug_suppressed_even_when_verbose(
        self, fresh_root_logger, monkeypatch, capsys,
    ):
        # Non-HTTP third parties still emit INFO under VERBOSE=true
        # (useful SDK startup / session records); DEBUG is still gone
        # because root sits at INFO.
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", True)
        main_mod._configure_logging()

        logging.getLogger("claude_agent_sdk").debug("packet dump nobody wants")
        logging.getLogger("claude_agent_sdk").info("session opened sid=abc")

        captured = capsys.readouterr()
        assert "packet dump" not in captured.out
        assert "session opened" in captured.out

    def test_httpx_info_suppressed_even_when_verbose(
        self, fresh_root_logger, monkeypatch, capsys,
    ):
        # HTTP wire-chatter (httpx et al) is the only class of third party
        # explicitly capped at WARNING: INFO-level "POST ... 200 OK" is
        # pure noise that floods the CLI on the WeChat long-poll path.
        # Errors still surface (logged at WARNING+).
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", True)
        main_mod._configure_logging()

        logging.getLogger("httpx").info("HTTP Request: POST /getupdates 200")
        logging.getLogger("httpx").warning("connection reset by peer")

        captured = capsys.readouterr()
        assert "getupdates 200" not in captured.out
        assert "connection reset" in captured.out

    def test_pip_agent_info_reaches_stdout_when_not_verbose(
        self, fresh_root_logger, monkeypatch, capsys,
    ):
        # Quiet mode must still surface pip_agent INFO — startup /
        # scheduler / channel context. The pane going dark during a
        # healthy run is a direct symptom of this breaking.
        from pip_agent import __main__ as main_mod

        monkeypatch.setattr(main_mod.settings, "verbose", False)
        main_mod._configure_logging()

        logging.getLogger("pip_agent.host_scheduler").debug("debug hidden")
        logging.getLogger("pip_agent.host_scheduler").info("info visible")
        logging.getLogger("pip_agent.host_scheduler").warning("warning visible")
        logging.getLogger("httpx").info("httpx hidden")
        logging.getLogger("claude_agent_sdk").info("third-party hidden")

        captured = capsys.readouterr()
        assert "debug hidden" not in captured.out
        assert "info visible" in captured.out
        assert "warning visible" in captured.out
        assert "httpx hidden" not in captured.out
        assert "third-party hidden" not in captured.out


# ---------------------------------------------------------------------------
# main() regression tripwire
# ---------------------------------------------------------------------------


class TestMainWiresLoggingBeforeRunHost:
    """If this class starts failing, someone removed the
    ``_configure_logging()`` call from ``main()`` (or moved it after
    ``run_host``). Do not ``xfail`` it — fix the call site.
    """

    def test_logging_is_configured_before_run_host_under_verbose(
        self, fresh_root_logger, monkeypatch,
    ):
        from pip_agent import __main__ as main_mod

        observed: dict[str, object] = {}

        def _fake_run_host(**kwargs: object) -> None:
            observed["root_has_handler"] = bool(logging.getLogger().handlers)
            observed["root_level"] = logging.getLogger().level
            observed["pip_agent_level"] = logging.getLogger("pip_agent").level

        # main() does a late import of run_host from pip_agent.agent_host,
        # so patch it at the module attribute, not in main_mod.
        monkeypatch.setattr("pip_agent.agent_host.run_host", _fake_run_host)
        monkeypatch.setattr(main_mod.settings, "verbose", True)

        main_mod.main([])

        assert observed.get("root_has_handler"), (
            "main() invoked run_host without configuring logging — "
            "every log.info/debug from pip_agent will be silently dropped"
        )
        assert observed["root_level"] == logging.INFO, (
            "root logger level did not track settings.verbose; "
            "_configure_logging contract broken"
        )
        assert observed["pip_agent_level"] == logging.DEBUG, (
            "pip_agent logger was not bumped to DEBUG under VERBOSE=true; "
            "our own DEBUG output will be swallowed"
        )

    def test_logging_is_configured_before_run_host_under_quiet(
        self, fresh_root_logger, monkeypatch,
    ):
        # Quiet mode must still install a handler — the difference is the
        # threshold, not presence. Otherwise WARNING/ERROR records from
        # pip_agent would also disappear.
        from pip_agent import __main__ as main_mod

        observed: dict[str, object] = {}

        def _fake_run_host(**kwargs: object) -> None:
            observed["root_has_handler"] = bool(logging.getLogger().handlers)
            observed["root_level"] = logging.getLogger().level

        monkeypatch.setattr("pip_agent.agent_host.run_host", _fake_run_host)
        monkeypatch.setattr(main_mod.settings, "verbose", False)

        main_mod.main([])

        assert observed.get("root_has_handler"), "quiet mode dropped the handler"
        assert observed["root_level"] == logging.WARNING
