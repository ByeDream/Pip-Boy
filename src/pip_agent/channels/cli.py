"""Local-terminal channel.

The CLI channel is trivial — ``stdout`` can't fail, doesn't need chunking,
and doesn't carry any inbound bookkeeping. Kept in its own module purely
so :mod:`pip_agent.channels.base` stays free of terminal-specific I/O.

In TUI mode the ``send`` path routes the reply through
:func:`pip_agent.host_io.emit_agent_markdown` so the active App
renders it as a markdown block; in line mode it falls back to the
historical separator + print behaviour.
"""
from __future__ import annotations

from typing import Any

from pip_agent.channels.base import Channel


class CLIChannel(Channel):
    name = "cli"

    def send(self, to: str, text: str, *, account_id: str = "", **kw: Any) -> bool:
        # ``host_io`` owns the print/sink branch; importing here keeps
        # the CLI channel module load-time free of TUI dependencies.
        from pip_agent.host_io import emit_agent_markdown, is_tui_active

        if is_tui_active():
            emit_agent_markdown(text)
        else:
            print()
            print("================================================")
            print(text)
        return True
