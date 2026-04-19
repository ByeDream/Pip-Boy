import argparse
import sys

from pip_agent.config import ConfigError


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

    try:
        from pip_agent.agent_host import run_host
        run_host(mode=args.mode, bind_agent=args.bind)
    except ConfigError as exc:
        print(f"  [config error] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
