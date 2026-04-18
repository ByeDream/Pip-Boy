import argparse
import sys

from pip_agent.agent import run
from pip_agent.config import ConfigError


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pip-boy")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--scan", nargs="?", const=True, default=False,
        help="Force WeChat QR login; optionally specify agent name to bind",
    )
    group.add_argument("--cli", action="store_true", help="CLI-only mode")
    group.add_argument("--version", action="store_true", help="Show version and exit")
    args = parser.parse_args(argv)

    if args.version:
        from pip_agent import __version__
        print(f"pip-boy {__version__}")
        return

    try:
        if args.cli:
            run(mode="cli")
        elif args.scan:
            bind_agent = args.scan if isinstance(args.scan, str) else None
            run(mode="scan", bind_agent=bind_agent)
        else:
            run(mode="auto")
    except ConfigError as exc:
        print(f"  [config error] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
