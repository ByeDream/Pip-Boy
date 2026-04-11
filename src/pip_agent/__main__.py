import argparse

from pip_agent.agent import run

parser = argparse.ArgumentParser(prog="pip-agent")
group = parser.add_mutually_exclusive_group()
group.add_argument("--scan", action="store_true", help="Force WeChat QR login")
group.add_argument("--cli", action="store_true", help="CLI-only mode")
args = parser.parse_args()

if args.cli:
    run(mode="cli")
elif args.scan:
    run(mode="scan")
else:
    run(mode="auto")
