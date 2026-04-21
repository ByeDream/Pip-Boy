import argparse
import logging
import sys

from pip_agent.config import ConfigError, settings


def _configure_logging() -> None:
    """Route internal log records to stdout.

    ``VERBOSE`` is the single "open the log firehose" switch. It controls
    **only** the stdlib ``logging`` threshold — it does **not** gate
    streaming agent replies or tool-use traces, because those are part of
    the interactive CLI contract and must show regardless of log volume.

    Layout (two tiers, no per-library pins):

    * ``VERBOSE=true``
        - root logger at ``INFO`` — third-party libs (``mcp``, ``httpx``,
          ``httpcore``, ``claude_agent_sdk``, ``asyncio``, …) emit INFO+
          only. Their DEBUG layers stay hidden, which keeps the output
          readable and also silences the SDK's best-effort
          ``OTEL trace context injection failed`` DEBUG traceback.
        - ``pip_agent.*`` at ``DEBUG`` — scheduler ticks, heartbeat
          sentinel suppression, memory pipeline state, hook invocations,
          and every other ``log.debug`` we ship. This is the signal you
          actually want from "firehose mode".
    * ``VERBOSE=false`` (default)
        - root logger at ``WARNING``. Errors, channel-level failures, and
          the agent's own text output still show; the "plumbing is doing
          its job" chatter does not. ``pip_agent.*`` inherits root, so
          our DEBUG/INFO is hidden too.

    Regression guard: ``tests/test_main_logging.py`` asserts that ``main()``
    always passes through here before invoking ``run_host``. Historically,
    multiple refactors silently dropped that call site — every ``log.*``
    in the codebase went dark and the host looked dead.
    """
    root_level = logging.INFO if settings.verbose else logging.WARNING
    # ``force=True`` so we override any stale basicConfig left behind by an
    # earlier import or a test harness (pytest's logging plugin installs its
    # own root handler which would otherwise make this call a silent no-op).
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    if settings.verbose:
        # Only Pip-Boy's own modules go to DEBUG; third parties stay at the
        # root level (INFO) so their internals don't drown our signal.
        logging.getLogger("pip_agent").setLevel(logging.DEBUG)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pip-boy")
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    parser.add_argument(
        "--mode", choices=["auto", "cli", "scan"],
        default="auto",
        help="Channel mode: auto (connect all available), cli (CLI only), scan (force WeChat QR)",
    )
    parser.add_argument("--bind", default=None, help="Bind WeChat channel to a specific agent ID")
    args = parser.parse_args(argv)

    if args.version:
        from pip_agent import __version__
        print(f"pip-boy {__version__}")
        return

    # Order matters: UTF-8 console BEFORE logging. ``basicConfig`` captures
    # ``sys.stdout`` into a ``StreamHandler``; if we detach stdout afterward
    # the handler keeps writing into a dead wrapper and every ``log.*`` call
    # raises ``ValueError: underlying buffer has been detached``.
    from pip_agent.console_io import force_utf8_console

    force_utf8_console()
    _configure_logging()

    try:
        from pip_agent.agent_host import run_host
        run_host(mode=args.mode, bind_agent=args.bind)
    except ConfigError as exc:
        print(f"  [config error] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
