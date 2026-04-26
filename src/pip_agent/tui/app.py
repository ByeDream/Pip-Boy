"""``PipBoyTuiApp`` — the locked TUI widget topology.

Layout (LOCKED — themes can hide widgets via the manifest's
``show_*`` flags but cannot rearrange them):

::

    Screen
    ├── #status-bar       1 row, dock top
    └── #main             horizontal flex
        ├── #agent-pane   3fr — model dialog + input
        │   ├── #agent-log
        │   └── #input
        └── #side-pane    1fr — art + app log
            ├── #pipboy-art
            └── #app-log

Three message handlers, one per sink, fed by
:class:`pip_agent.tui.pump.UiPump`. The App never reads ``sys.stdin``
or writes to ``sys.stdout`` directly; everything goes through
widgets.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.widgets import Input, RichLog, Static

from pip_agent.tui.messages import AgentMessage, LogMessage, StatusMessage
from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent
from pip_agent.tui.textual_theme import textual_theme_from_bundle
from pip_agent.tui.theme_api import ThemeBundle

__all__ = ["PipBoyTuiApp"]


# Type alias for the host hook: the app forwards every submitted line
# (and only that — no /exit short-circuit; design.md §6) to this
# callable, which the host wires to its inbound queue.
UserLineHandler = Callable[[str], Awaitable[None] | None]


def _rich_log_strip_tail(log_widget: RichLog, n: int) -> None:
    """Remove the last ``n`` rendered strips and fix RichLog geometry.

    Used to replace the streaming assistant tail in-place so the reply
    stays inside ``#agent-log`` instead of a separate widget below it.

    Couples to Textual's ``RichLog`` internals (``lines``, ``_line_cache``,
    ``_widest_line_width``) — revisit if Textual refactors the widget.
    """
    if n <= 0:
        return
    lines = log_widget.lines
    take = min(n, len(lines))
    if take:
        del lines[-take:]
    log_widget._line_cache.clear()
    if not lines:
        log_widget._widest_line_width = 0
    else:
        log_widget._widest_line_width = max(s.cell_length for s in lines)
    log_widget.virtual_size = Size(log_widget._widest_line_width, len(lines))
    log_widget.refresh()


class PipBoyTuiApp(App[None]):
    """Top-level Textual App for Pip-Boy.

    The constructor takes a *theme bundle* (loaded by Phase A's
    :func:`pip_agent.tui.loader.load_builtin_theme`; Phase B will
    swap in :class:`pip_agent.tui.theme_api.ThemeManager`), a
    :class:`UiPump` (the producer-side fan-in), and a callable for
    forwarding user input lines back to the host's inbound queue.

    The App never imports anything from ``pip_agent.agent_host``;
    that's deliberate — the App is a pure view/control surface, not
    a host integration point. Phase A.3 wires the host to the App
    via constructor injection.
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_log", "Clear log"),
    ]

    def __init__(
        self,
        *,
        theme: ThemeBundle,
        pump: UiPump,
        on_user_line: UserLineHandler | None = None,
    ) -> None:
        # The TCSS file isn't reachable from the package data path at
        # ``CSS_PATH`` time (Textual reads it before mount); we attach
        # it via ``CSS`` (raw stylesheet text) instead, which is the
        # supported route for stylesheets bundled inside non-package
        # data. ``stylesheet`` is the runtime equivalent.
        super().__init__()
        self._theme = theme
        self._pump = pump
        self._on_user_line = on_user_line

        # Map ``theme.toml`` palette onto Textual's design tokens. Without
        # this, ``$accent`` / ``$surface`` resolve from ``textual-dark``
        # (orange accent) while the status line still prints the
        # manifest display name — a misleading split.
        _tt = textual_theme_from_bundle(theme)
        self.register_theme(_tt)
        self.theme = _tt.name

        # ``text_delta`` is often chunked per character. Buffer here and
        # rewrite the *tail* of ``#agent-log`` on each chunk so the reply
        # stays one growing block (no per-char rows; no extra pane jump).
        self._stream_buf: str = ""
        self._stream_tail_strips: int = 0
        self._streaming_open = False

    # ------------------------------------------------------------------
    # Stylesheets
    # ------------------------------------------------------------------

    @property
    def CSS(self) -> str:  # type: ignore[override]
        """Inject the active theme's TCSS at App construction time.

        Textual reads ``self.CSS`` once during ``App.__init__``; using
        a property here is fine because the value is captured into the
        App's stylesheet on first access. Themes never change at
        runtime in v1 (design.md §B "live reload v1 不做"), so the
        property is effectively a read-once constant.

        A short tail overrides ``#status-bar`` colours with the
        manifest's ``status_bar`` / ``status_bar_text`` tokens so the
        bar does not inherit generic ``$boost`` / ``$text`` shades.
        """
        p = self._theme.manifest.palette
        tail = (
            "\n/* Manifest palette: status bar */\n"
            f"#status-bar {{\n"
            f"    background: {p.status_bar};\n"
            f"    color: {p.status_bar_text};\n"
            f"}}\n"
        )
        return self._theme.tcss + tail

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the locked widget tree.

        ``show_*`` toggles from the manifest hide widgets but never
        change their IDs or relative ordering — snapshot baselines
        rely on the topology being identical across themes.
        """
        manifest = self._theme.manifest

        if manifest.show_status_bar:
            yield Static(self._status_default_text(), id="status-bar")

        with Horizontal(id="main"):
            with Vertical(id="agent-pane"):
                yield RichLog(
                    id="agent-log",
                    highlight=False,
                    markup=True,
                    wrap=True,
                    min_width=0,
                    auto_scroll=True,
                )
                yield Input(
                    placeholder="Type and press Enter — /exit to quit",
                    id="input",
                )
            if manifest.show_app_log or self._theme.art:
                with Vertical(id="side-pane"):
                    if manifest.show_art and self._theme.art:
                        yield Static(self._theme.art, id="pipboy-art")
                    if manifest.show_app_log:
                        yield RichLog(
                            id="app-log",
                            highlight=False,
                            markup=False,
                            wrap=True,
                            min_width=0,
                            auto_scroll=True,
                        )

    def on_mount(self) -> None:
        """Attach the pump after every widget is mounted.

        Order matters: ``attach`` flushes the buffered banner /
        scaffold events into the App's message queue, and those
        handlers need ``query_one`` to resolve. Calling ``attach``
        before mount would race the very events it's supposed to
        deliver.
        """
        self._pump.attach(self)
        try:
            self.query_one("#input", Input).focus()
        except Exception:  # pragma: no cover — input always present in v1
            pass

    # ------------------------------------------------------------------
    # Pump message handlers
    # ------------------------------------------------------------------

    def on_agent_message(self, message: AgentMessage) -> None:
        """Render one agent-pane event."""
        try:
            log_widget = self.query_one("#agent-log", RichLog)
        except Exception:
            return
        event = message.event
        if event.kind == "user_input":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            log_widget.write(Text(f"> {event.text}", style="bold"))
        elif event.kind == "thinking_delta":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            log_widget.write(
                Text(event.text.rstrip("\n"), style="dim italic")
            )
        elif event.kind == "text_delta":
            self._streaming_open = True
            self._stream_buf += event.text
            self._rewrite_stream_tail(log_widget)
        elif event.kind == "plain":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            log_widget.write(Text(event.text or ""), expand=True)
        elif event.kind == "tool_use":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            args = f" {event.text}" if event.text else ""
            log_widget.write(
                Text(f"[tool: {event.name}{args}]", style="cyan")
            )
        elif event.kind == "markdown":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            log_widget.write(
                Markdown(event.text or "", justify="left"),
            )
        elif event.kind == "finalize":
            # Streaming used ``Text`` for stable tail rewrites; swap the
            # finished buffer for ``Markdown`` once so ** / ` / lists
            # render instead of showing raw control characters.
            self._flush_stream_buffer(log_widget, materialize_markdown=True)
            footer = self._format_footer(event)
            self._streaming_open = False
            log_widget.write(Text(footer, style="dim"))
        elif event.kind == "error":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            log_widget.write(Text(f"[error] {event.text}", style="bold red"))

    def on_log_message(self, message: LogMessage) -> None:
        """Render one stdlib log record into the app-log pane."""
        try:
            log_widget = self.query_one("#app-log", RichLog)
        except Exception:
            return
        record = message.record
        line = self._format_log_record(record)
        if record.levelno >= logging.ERROR:
            log_widget.write(Text(line, style="bold red"))
        elif record.levelno >= logging.WARNING:
            log_widget.write(Text(line, style="yellow"))
        else:
            log_widget.write(Text(line, style="dim"))

    def on_status_message(self, message: StatusMessage) -> None:
        """Render one status-bar update."""
        event = message.event
        if event.kind in {"banner", "ready", "channel_ready", "scheduler"}:
            text = event.text
        elif event.kind == "channel_lost":
            text = f"[!] {event.text}"
        elif event.kind == "shutdown":
            text = f"powering down — {event.text}".rstrip(" —")
        else:  # pragma: no cover — kind enum-checked at construction
            text = event.text
        try:
            status_bar = self.query_one("#status-bar", Static)
            status_bar.update(text)
        except Exception:
            try:
                log_fallback = self.query_one("#agent-log", RichLog)
                self._flush_stream_buffer(log_fallback)
                log_fallback.write(Text(text, style="bold green"))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Forward each submitted line to the host's inbound queue.

        The TUI does NOT short-circuit ``/exit`` — design.md §6
        explicitly forbids that. ``/exit`` flows through the host
        like any other command so ``flush_and_rotate`` runs and
        observations are not lost. The host calls :meth:`request_exit`
        once teardown completes; only then does the App actually
        terminate.
        """
        text = event.value
        try:
            self.query_one("#input", Input).clear()
        except Exception:
            pass
        if not text.strip():
            return
        if self._on_user_line is not None:
            try:
                result = self._on_user_line(text)
                if result is not None and hasattr(result, "__await__"):
                    self.run_worker(result, exclusive=False)
            except Exception:
                logging.getLogger(__name__).exception(
                    "on_user_line handler raised; suppressing to keep "
                    "TUI responsive."
                )

    # ------------------------------------------------------------------
    # External shutdown signal
    # ------------------------------------------------------------------

    def request_exit(self) -> None:
        """Tell the App to exit. Called by the host after teardown.

        This is the *only* path the host should use to stop the TUI —
        calling :meth:`App.exit` directly from a sink or worker thread
        bypasses the pump's thread-safety contract.
        """
        self.call_later(self.exit)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_clear_log(self) -> None:
        """``Ctrl+L`` clears the agent log only — app log is preserved."""
        self._stream_buf = ""
        self._stream_tail_strips = 0
        try:
            self.query_one("#agent-log", RichLog).clear()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _status_default_text(self) -> str:
        m = self._theme.manifest
        return f"Pip-Boy — theme: {m.display_name} v{m.version}"

    def _flush_stream_buffer(
        self, log_widget: RichLog, *, materialize_markdown: bool = False,
    ) -> None:
        """Drop streaming bookkeeping; optionally re-render the tail as Markdown."""
        buf = self._stream_buf
        n = self._stream_tail_strips
        if materialize_markdown and buf.strip() and n > 0:
            _rich_log_strip_tail(log_widget, n)
            log_widget.write(Markdown(buf, justify="left"))
        self._stream_buf = ""
        self._stream_tail_strips = 0

    def _rewrite_stream_tail(self, log_widget: RichLog) -> None:
        buf = self._stream_buf
        if not buf:
            self._stream_tail_strips = 0
            return
        _rich_log_strip_tail(log_widget, self._stream_tail_strips)
        before = len(log_widget.lines)
        log_widget.write(Text(buf), expand=True)
        self._stream_tail_strips = len(log_widget.lines) - before

    def _format_footer(self, event: AgentEvent) -> str:
        template = self._theme.manifest.footer_template
        cost_str = f"{event.cost_usd:.4f}" if event.cost_usd else "0.0000"
        usage = event.usage or {}
        try:
            return template.format(
                turns=event.num_turns,
                cost=cost_str,
                elapsed_s=f"{event.elapsed_s:.1f}",
                tokens_in=usage.get("input_tokens", 0),
                tokens_out=usage.get("output_tokens", 0),
                tools=usage.get("tool_calls", 0),
            )
        except (KeyError, IndexError):
            return f"[turns={event.num_turns} cost=${cost_str}]"

    @staticmethod
    def _format_log_record(record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except Exception:
            msg = record.msg if isinstance(record.msg, str) else repr(record.msg)
        return f"{record.levelname:<7} {record.name}: {msg}"
