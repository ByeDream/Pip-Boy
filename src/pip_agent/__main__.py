import argparse
import sys

from pip_agent.config import ConfigError


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pip-boy")
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    args = parser.parse_args(argv)

    if args.version:
        from pip_agent import __version__
        print(f"pip-boy {__version__}")
        return

    try:
        from pip_agent.agent_cli import run_sdk_cli
        run_sdk_cli()
    except ConfigError as exc:
        print(f"  [config error] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
