import argparse
import logging
import sys

from pip_agent.config import settings

# HTTP-layer loggers whose INFO records are pure wire-chatter
# (one line per request, no useful signal). The WeChat iLink long-poll
# returns ~20 req/sec in the quiet path, which makes the stdout
# unusable under ``VERBOSE=true`` — you literally cannot type a message
# into the CLI because the prompt scrolls off the screen every 50ms.
#
# These libs are pinned at WARNING unconditionally: errors, timeouts,
# and retries still surface; ``HTTP/1.1 200 OK`` does not.
_HTTP_NOISY_LIBS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "h11",
)


def _configure_logging() -> None:
    """Route internal log records to stdout.

    ``VERBOSE`` is the "open the DEBUG firehose for Pip-Boy's own code"
    switch. It does NOT change third-party libraries — those always ride
    the root logger so flipping ``VERBOSE`` never surprises the operator
    with an SDK/MCP DEBUG flood.

    Layout (with a WARNING cap on HTTP wire-chatter libs):

    * ``VERBOSE=true``
        - root logger at ``INFO`` — third-party libs (``mcp``,
          ``claude_agent_sdk``, ``asyncio``, ``anyio``, …) emit INFO+
          only. Their DEBUG layers stay hidden, which keeps the output
          readable and also silences the SDK's best-effort
          ``OTEL trace context injection failed`` DEBUG traceback.
        - ``pip_agent.*`` at ``DEBUG`` — scheduler ticks, heartbeat
          sentinel suppression, memory pipeline state, hook invocations,
          and every other ``log.debug`` we ship. This is the signal you
          actually want from "firehose mode".
    * ``VERBOSE=false`` (default)
        - root logger at ``WARNING`` — third-party libs only surface
          WARNING/ERROR. The "plumbing is doing its job" chatter stays
          hidden.
        - ``pip_agent.*`` at ``INFO`` — Pip-Boy's own startup / channel /
          scheduler records still reach stdout, the TUI ``#app-log``
          pane, and ``pip-boy.log``. Only DEBUG is suppressed, which is
          the actual purpose of quiet mode.

    In both tiers ``httpx`` / ``httpcore`` / ``urllib3`` / ``h11`` are
    pinned at ``WARNING``: their INFO is one line per HTTP request and
    the WeChat long-poll fires ~20 req/sec when the server fast-returns,
    which floods stdout and renders the CLI prompt unusable. Errors
    still surface at WARNING+.

    Regression guard: ``tests/test_main_logging.py`` asserts that ``main()``
    always passes through here before invoking ``run_host``. Historically,
    multiple refactors silently dropped that call site — every ``log.*``
    in the codebase went dark and the host looked dead.
    """
    if settings.verbose:
        root_level = logging.INFO
        pip_agent_level = logging.DEBUG
    else:
        root_level = logging.WARNING
        pip_agent_level = logging.INFO

    # ``force=True`` so we override any stale basicConfig left behind by an
    # earlier import or a test harness (pytest's logging plugin installs its
    # own root handler which would otherwise make this call a silent no-op).
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("pip_agent").setLevel(pip_agent_level)

    for name in _HTTP_NOISY_LIBS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pip-boy")
    parser.add_argument(
        "--version", action="store_true", help="Show version and exit",
    )
    # Operator-side TUI override. The capability ladder
    # (:mod:`pip_agent.tui.capability`) defaults to "TUI on if the
    # terminal supports it"; ``--no-tui`` is the explicit opt-out for
    # the cases where the operator knows ahead of time the TUI won't
    # work — CI, ``pip-boy < script.txt`` runs, terminals with broken
    # CJK rendering, etc. ``cli_layout`` config flags are deliberately
    # NOT introduced (PipBoyCLITheme/design.md §5).
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Skip TUI bootstrap and run in line mode regardless of "
             "terminal capability.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without TUI, stdin, or CLI channel. "
             "Only remote channels (WeCom/WeChat) are active. "
             "Suitable for unattended server deployments.",
    )

    # ``doctor`` is dispatched as a sub-parser instead of an action
    # flag because PipBoyCLITheme/design.md §C makes it a separate
    # command surface — read-only, must run without launching the
    # host, and survives whatever the rest of the env is. Sub-parsers
    # also leave room for future ``pip-boy theme list`` etc. without
    # reshuffling the top-level argparse layout.
    subparsers = parser.add_subparsers(dest="command")
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Print an environment + TUI self-check report and exit.",
    )
    doctor_parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Force the capability ladder shown in the report to "
             "short-circuit at user_optout.",
    )
    args = parser.parse_args(argv)

    if args.version:
        from pip_agent import __version__
        print(f"pip-boy {__version__}")
        return

    if args.command == "doctor":
        # The doctor is intentionally side-effect free: no logging
        # bring-up, no UTF-8 console coercion (it should describe
        # whatever the environment looks like, not "fix" it), no
        # workspace scaffolding. Just read what's there and print.
        from pathlib import Path

        from pip_agent.doctor import run_doctor

        sys.exit(run_doctor(
            workdir=Path.cwd(), force_no_tui=args.no_tui,
        ))

    # Order matters: UTF-8 console BEFORE logging. ``basicConfig`` captures
    # ``sys.stdout`` into a ``StreamHandler``; if we detach stdout afterward
    # the handler keeps writing into a dead wrapper and every ``log.*`` call
    # raises ``ValueError: underlying buffer has been detached``.
    from pip_agent.console_io import force_utf8_console

    force_utf8_console()
    _configure_logging()

    # Initialise profiling as early as possible so the first cold-start
    # milestone has somewhere to land. No-op when ``enable_profiler`` is
    # false, which is the default.
    from pip_agent import _profile

    _profile.bootstrap()
    _profile.cold_start("logging_ready")

    from pip_agent.agent_host import run_host
    _profile.cold_start("run_host_imported")
    run_host(
        force_no_tui=args.no_tui,
        headless=getattr(args, "headless", False),
    )


if __name__ == "__main__":
    main()
