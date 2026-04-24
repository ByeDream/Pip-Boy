"""Local-terminal channel.

The CLI channel is trivial — ``stdout`` can't fail, doesn't need chunking,
and doesn't carry any inbound bookkeeping. Kept in its own module purely
so :mod:`pip_agent.channels.base` stays free of terminal-specific I/O.
"""
from __future__ import annotations

from typing import Any

from pip_agent.channels.base import Channel


class CLIChannel(Channel):
    name = "cli"

    def send(self, to: str, text: str, *, account_id: str = "", **kw: Any) -> bool:
        print()
        print("================================================")
        print(text)
        return True
