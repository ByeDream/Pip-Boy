import argparse
import sys

from pip_agent.agent import run


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pip-boy")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scan", action="store_true", help="Force WeChat QR login")
    group.add_argument("--cli", action="store_true", help="CLI-only mode")
    group.add_argument("--version", action="store_true", help="Show version and exit")
    args = parser.parse_args(argv)

    if args.version:
        from pip_agent import __version__
        print(f"pip-boy {__version__}")
        return

    if args.cli:
        run(mode="cli")
    elif args.scan:
        run(mode="scan")
    else:
        run(mode="auto")


if __name__ == "__main__":
    main()
