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

    ``VERBOSE`` is the single "open the log firehose" switch. It controls
    **only** the stdlib ``logging`` threshold — it does **not** gate
    streaming agent replies or tool-use traces, because those are part of
    the interactive CLI contract and must show regardless of log volume.

    Layout (two tiers, with a WARNING cap on HTTP wire-chatter libs):

    * ``VERBOSE=true``
        - root logger at ``INFO`` — most third-party libs (``mcp``,
          ``claude_agent_sdk``, ``asyncio``, ``anyio``, …) emit INFO+
          only. Their DEBUG layers stay hidden, which keeps the output
          readable and also silences the SDK's best-effort
          ``OTEL trace context injection failed`` DEBUG traceback.
        - ``pip_agent.*`` at ``DEBUG`` — scheduler ticks, heartbeat
          sentinel suppression, memory pipeline state, hook invocations,
          and every other ``log.debug`` we ship. This is the signal you
          actually want from "firehose mode".
        - ``httpx`` / ``httpcore`` / ``urllib3`` / ``h11`` capped at
          ``WARNING``. Their INFO is one line per HTTP request; the
          WeChat long-poll fires ~20 req/sec when the server fast-returns,
          which floods the CLI and makes the input prompt unusable.
          Errors still surface because they log at WARNING+.
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

    # Cap HTTP wire-chatter at WARNING regardless of VERBOSE. See the
    # module-level comment on ``_HTTP_NOISY_LIBS`` for the operational
    # reason this can't be left to ride root.
    for name in _HTTP_NOISY_LIBS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pip-boy")
    parser.add_argument(
        "--version", action="store_true", help="Show version and exit",
    )
    parser.add_argument(
        "--wechat", metavar="AGENT_ID", default=None,
        help=(
            "Log a new WeChat account in via QR and bind it to AGENT_ID. "
            "The scan runs in the background so the CLI stays usable "
            "(/wechat cancel aborts the scan, /exit aborts everything)."
        ),
    )
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

    # Initialise profiling as early as possible so the first cold-start
    # milestone has somewhere to land. No-op when ``enable_profiler`` is
    # false, which is the default.
    from pip_agent import _profile

    _profile.bootstrap()
    _profile.cold_start("logging_ready", wechat=bool(args.wechat))

    from pip_agent.agent_host import run_host
    _profile.cold_start("run_host_imported")
    run_host(wechat_login_for=args.wechat)


if __name__ == "__main__":
    main()
