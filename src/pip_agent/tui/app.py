"""``PipBoyTuiApp`` — the TUI widget topology.

Layout:

::

    Screen
    ├── #status-bar              1 row, dock top
    └── #main                    horizontal flex
        ├── #agent-pane          1fr — model dialog + input
    │   ├── #agent-log         1fr — user input + assistant text
    │   ├── #agent-log-detail  14 rows — thinking / tool / finalize
        │   └── #input
        └── #side-pane           fixed width (set per-theme in TCSS)
            ├── #side-top        auto height (art + clock)
            │   ├── #pipboy-art
            │   └── #pipboy-clock
            ├── #side-status     auto height (snapshot of host state)
            ├── #todo-pane       auto height (TodoWrite task list)
            └── #app-log         1fr — stdlib log mirror

Three message handlers, one per sink, fed by
:class:`pip_agent.tui.pump.UiPump`. The App never reads ``sys.stdin``
or writes to ``sys.stdout`` directly; everything goes through
widgets.

The topology above is considered stable — themes can hide widgets
via the manifest's ``show_*`` flags and swap ``ascii_art_*.txt``
frames, but cannot rearrange or add/remove containers. Snapshot
baselines guard the structural shape.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Awaitable, Callable

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.widgets import Input, RichLog, Static

from pip_agent.tui.messages import AgentMessage, LogMessage, StatusMessage
from pip_agent.tui.modals import AskUserModal, PlanReviewModal
from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent
from pip_agent.tui.textual_theme import textual_theme_from_bundle
from pip_agent.tui.theme_api import ThemeBundle
from pip_agent.tui.tool_format import format_tool_detail, format_tool_summary

__all__ = ["PipBoyTuiApp"]


# Type alias for the host hook: the app forwards every submitted line
# (and only that — no /exit short-circuit; design.md §6) to this
# callable, which the host wires to its inbound queue.
UserLineHandler = Callable[[str], Awaitable[None] | None]

# Type alias for the injectable clock provider. Tests and snapshot
# scenarios pass a fixed-time callable so the baselines are
# deterministic; production passes ``None`` and the app falls back to
# :func:`datetime.now`.
ClockProvider = Callable[[], datetime]

# Type alias for the periodic #side-status snapshot refresher. The
# App re-invokes this every ``snapshot_refresh_interval`` seconds and
# merges the returned dict into its cached snapshot. Returning ``{}``
# leaves the current view intact; returning a partial dict updates
# only the named fields.
SnapshotProvider = Callable[[], dict[str, str]]


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
        clock_provider: ClockProvider | None = None,
        initial_side_snapshot: dict[str, str] | None = None,
        snapshot_provider: SnapshotProvider | None = None,
        snapshot_refresh_interval: float = 5.0,
        art_anim_interval: float = 3.0,
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
        self._clock_provider = clock_provider
        self._side_snapshot: dict[str, str] = dict(initial_side_snapshot or {})
        self._snapshot_provider = snapshot_provider
        self._snapshot_refresh_interval = snapshot_refresh_interval

        # Art animation state.
        self._art_frames: tuple[str, ...] = theme.art_frames
        self._art_frame_idx: int = 0
        self._art_anim_interval: float = art_anim_interval

        # Map ``theme.toml`` palette onto Textual's design tokens. Without
        # this, ``$accent`` / ``$surface`` resolve from ``textual-dark``
        # (orange accent) while the status line still prints the
        # manifest display name — a misleading split.
        _tt = textual_theme_from_bundle(theme)
        self.register_theme(_tt)
        self.theme = _tt.name

        # TodoWrite task list. Updated when the agent calls TodoWrite;
        # rendered into ``#todo-pane`` in the side panel. Hidden when
        # empty so the pane occupies zero vertical space until needed.
        self._todos: list[dict[str, str]] = []

        # ``text_delta`` is often chunked per character. Buffer here and
        # rewrite the *tail* of ``#agent-log`` on each chunk so the reply
        # stays one growing block (no per-char rows; no extra pane jump).
        self._stream_buf: str = ""
        self._stream_tail_strips: int = 0
        self._streaming_open = False
        # Same buffering for ``thinking_delta`` — each SDK chunk must not
        # become its own row; accumulate and rewrite the tail instead.
        self._think_buf: str = ""
        self._think_tail_strips: int = 0

    # ------------------------------------------------------------------
    # Stylesheets
    # ------------------------------------------------------------------

    @property
    def CSS(self) -> str:  # type: ignore[override]
        """The active theme's TCSS + a status-bar palette tail.

        Textual reads ``self.CSS`` once during ``App.__init__`` and
        captures it into ``self.stylesheet`` under the key
        ``(inspect.getfile(self.__class__), "PipBoyTuiApp.CSS")``.
        That's a one-shot read — refreshing the stylesheet later does
        *not* re-invoke this property. :meth:`apply_theme` handles
        the live-swap path by writing directly to
        ``self.stylesheet.add_source`` with the same key.
        """
        return self._compose_css(self._theme)

    @staticmethod
    def _compose_css(bundle: ThemeBundle) -> str:
        """Concatenate a bundle's TCSS with a status-bar palette tail.

        The tail hard-codes ``#status-bar`` colours from the manifest
        so the bar doesn't inherit generic ``$boost`` / ``$text`` shades
        from the Textual theme — those variables track the general
        palette but the status bar has dedicated tokens.
        """
        p = bundle.manifest.palette
        tail = (
            "\n/* Manifest palette: status bar */\n"
            f"#status-bar {{\n"
            f"    background: {p.status_bar};\n"
            f"    color: {p.status_bar_text};\n"
            f"}}\n"
        )
        return bundle.tcss + tail

    def _css_source_key(self) -> tuple[str, str]:
        """Key Textual uses to index ``self.CSS`` inside the stylesheet.

        Mirrors the tuple Textual builds when it first ingests the
        property (``app.py`` around line 3375). We need the exact same
        key so :meth:`apply_theme` can overwrite — not duplicate — the
        stylesheet entry when swapping themes at runtime.
        """
        try:
            app_path = inspect.getfile(self.__class__)
        except (TypeError, OSError):
            app_path = ""
        return (app_path, f"{self.__class__.__name__}.CSS")

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the TUI widget tree.

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
                yield RichLog(
                    id="agent-log-detail",
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
            # The side pane is shown when *any* of its sub-widgets have
            # content the theme wants visible. A theme that disables
            # all three (art, app_log, todo_pane) effectively hides the
            # pane entirely.
            side_has_art = manifest.show_art and bool(self._art_frames)
            side_visible = bool(
                side_has_art or manifest.show_app_log or manifest.show_todo_pane
            )
            if side_visible:
                with Vertical(id="side-pane"):
                    with Vertical(id="side-top"):
                        yield Static(
                            self._art_frames[0] if self._art_frames else "",
                            id="pipboy-art",
                        )
                        yield Static(
                            self._render_clock(), id="pipboy-clock"
                        )
                    yield Static(
                        self._render_side_status(), id="side-status"
                    )
                    yield Static("", id="todo-pane")
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

        # #todo-pane starts hidden (empty todo list); _refresh_todo_pane
        # will show it once a TodoWrite event arrives.
        self._refresh_todo_pane()

        # Art widget height: min(art_frame_height + 2, 30) — no lower
        # bound, so small art (e.g. 12 rows) sits in a 14-row frame
        # instead of floating in a 40-row empty block.
        try:
            art_h = min(30, self._theme.art_frame_height + 2)
            art_widget = self.query_one("#pipboy-art", Static)
            art_widget.styles.height = art_h
            if self._art_frames:
                art_widget.update(self._center_art(self._art_frames[0], art_h))
        except Exception:
            pass

        # The clock repaints once per second. Snapshot scenarios pass
        # a frozen ``clock_provider`` so the baseline is deterministic;
        # in that mode we still run ``set_interval`` but the provider
        # keeps returning the same datetime, so the text never changes.
        self.set_interval(1.0, self._tick_clock)

        # Art animation: only start if there are multiple frames.
        if len(self._art_frames) > 1:
            self.set_interval(self._art_anim_interval, self._tick_art)

        # #side-status: one-shot refresh from the provider right away
        # (so the first render reflects live state, not the frozen
        # bootstrap dict) + periodic refresh thereafter.
        if self._snapshot_provider is not None:
            self._tick_side_status()
            self.set_interval(
                self._snapshot_refresh_interval, self._tick_side_status,
            )

    # ------------------------------------------------------------------
    # Pump message handlers
    # ------------------------------------------------------------------

    def on_agent_message(self, message: AgentMessage) -> None:
        """Render one agent-pane event.

        Two stacked panes:

        * ``#agent-log`` — *dialog* pane. User input (right-aligned) +
          assistant text + markdown + ``plain`` blocks. This is what
          a first-time viewer should see; conversational back-and-forth
          reads top-to-bottom without wading through internals.
        * ``#agent-log-detail`` — *detail* strip (10 rows, below
          dialog). Thinking deltas, tool traces, finalize footer,
          error frames. Dim styling, scrolls independently. Users
          who want to "see what the agent is doing" look down; users
          who only care about the conversation ignore this region.

        Each terminal emission is followed by a blank line so messages
        are visually separated without a border on every row.
        """
        try:
            log_widget = self.query_one("#agent-log", RichLog)
        except Exception:
            return
        try:
            detail_widget = self.query_one("#agent-log-detail", RichLog)
        except Exception:
            detail_widget = None
        event = message.event
        if event.kind == "user_input":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            # Right-align the user's line so conversation reads like
            # a chat transcript: user on the right, assistant on the
            # left. Chevron dropped — the justification + bold is
            # already enough to distinguish it.
            log_widget.write(
                Text(event.text, justify="right", style="bold"),
                expand=True,
            )
            log_widget.write(Text(""))  # blank separator row
        elif event.kind == "thinking_delta":
            self._streaming_open = False
            target = detail_widget or log_widget
            self._think_buf += event.text
            self._rewrite_think_tail(target)
        elif event.kind == "text_delta":
            if self._think_buf:
                # Thinking just ended — drop the tail into the detail
                # pane (where it lived while streaming) and move on to
                # the reply in the dialog pane.
                self._flush_think_buffer(detail_widget or log_widget)
            self._streaming_open = True
            self._stream_buf += event.text
            self._rewrite_stream_tail(log_widget)
        elif event.kind == "plain":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            log_widget.write(Text(event.text or ""), expand=True)
            log_widget.write(Text(""))
        elif event.kind == "tool_use":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            target = detail_widget or log_widget
            summary = format_tool_summary(event.name, event.tool_input)
            if not summary and event.text:
                summary = event.text
            args = f" {summary}" if summary else ""
            target.write(
                Text(f"[tool: {event.name}{args}]", style="cyan")
            )
            # Interactive tools (AskUserQuestion, ExitPlanMode) carry
            # payloads the user genuinely wants to see — the summary
            # truncates them to one line. Render the full question /
            # plan body as a separate indented block right below the
            # trace so the content stays grouped with its tool header.
            detail = format_tool_detail(event.name, event.tool_input)
            if detail:
                target.write(Text(detail, style="bright_yellow"))
            target.write(Text(""))
            # TodoWrite: update the side-panel todo list.
            if event.name == "TodoWrite" and isinstance(event.tool_input, dict):
                self._apply_todo_write(event.tool_input)
            # Interactive follow-up: for AskUserQuestion and
            # ExitPlanMode, pop a modal so the user can actually answer
            # / approve instead of staring at the dim trace. The modal
            # result (if any) is injected back into the chat as a
            # regular user_line — the agent sees it next turn.
            self._maybe_trigger_tool_modal(event)
        elif event.kind == "markdown":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            log_widget.write(
                Markdown(event.text or "", justify="left"),
            )
            log_widget.write(Text(""))
        elif event.kind == "finalize":
            # Streaming used ``Text`` for stable tail rewrites; swap the
            # finished buffer for ``Markdown`` once so ** / ` / lists
            # render instead of showing raw control characters.
            self._flush_stream_buffer(log_widget, materialize_markdown=True)
            footer = self._format_footer(event)
            self._streaming_open = False
            log_widget.write(Text(footer, style="dim"))
            log_widget.write(Text(""))
        elif event.kind == "error":
            self._flush_stream_buffer(log_widget)
            self._streaming_open = False
            target = detail_widget or log_widget
            target.write(Text(f"[error] {event.text}", style="bold red"))
            target.write(Text(""))

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
        elif event.kind == "tool_wait":
            # Empty text is the "tool finished" sentinel — restore the
            # bar to its default string rather than leaving it blank.
            text = event.text or self._status_default_text()
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
        # Echo into the dialog pane so the transcript reads like a chat:
        # user line right-aligned, assistant text left-aligned. Without
        # this the user's own turn was invisible in #agent-log and only
        # the assistant side of the conversation showed up.
        try:
            self._pump.agent_sink(AgentEvent(kind="user_input", text=text))
        except Exception:
            pass
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
    # Interactive tool modals
    # ------------------------------------------------------------------

    def _maybe_trigger_tool_modal(self, event: AgentEvent) -> None:
        """Push a modal for AskUserQuestion / ExitPlanMode tool_use events.

        Dismissed result is forwarded via ``_forward_modal_result`` —
        which routes it through ``_on_user_line`` so the next agent turn
        receives the user's answer as a normal chat message. No SDK
        permission round-trip; see ``OVERNIGHT_REPORT.md`` for rationale.

        Modals are best-effort: if the App hasn't finished mounting yet
        (``is_running`` False), or if ``push_screen`` raises for any
        reason, the trace still landed in the detail pane — we just
        don't interrupt the user with a modal in an unusable state.
        """
        if not getattr(self, "is_running", False):
            return
        if not isinstance(event.tool_input, dict) or not event.tool_input:
            return
        if event.name == "AskUserQuestion":
            questions = event.tool_input.get("questions")
            if not isinstance(questions, list) or not questions:
                return
            try:
                self.push_screen(
                    AskUserModal(questions),
                    callback=self._forward_modal_result,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "AskUserModal push failed; skipping.",
                )
        elif event.name == "ExitPlanMode":
            plan = event.tool_input.get("plan")
            if not isinstance(plan, str) or not plan.strip():
                return
            try:
                self.push_screen(
                    PlanReviewModal(plan),
                    callback=self._forward_modal_result,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "PlanReviewModal push failed; skipping.",
                )

    def _forward_modal_result(self, result: str | None) -> None:
        """Modal callback — feed the answer into the chat via on_user_line.

        ``None`` means the user cancelled (Esc); in that case we emit
        a user_input event so the transcript still records that the
        question was declined, but nothing is forwarded to the agent.
        Non-empty answers run through the same path as typing in
        ``#input``, so behaviour is identical from the host's POV.
        """
        if result is None:
            try:
                self._pump.agent_sink(
                    AgentEvent(kind="user_input", text="(cancelled)")
                )
            except Exception:
                pass
            return
        if not result.strip():
            return
        # Echo the answer into the dialog pane so the user sees what was
        # sent, then forward it via the same handler the Input widget
        # uses. The echo is cosmetic; the handler call is what actually
        # drives the next agent turn.
        try:
            self._pump.agent_sink(
                AgentEvent(kind="user_input", text=result)
            )
        except Exception:
            pass
        if self._on_user_line is not None:
            try:
                inner = self._on_user_line(result)
                if inner is not None and hasattr(inner, "__await__"):
                    self.run_worker(inner, exclusive=False)
            except Exception:
                logging.getLogger(__name__).exception(
                    "modal on_user_line forwarder raised; suppressing.",
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
    # Runtime theme swap
    # ------------------------------------------------------------------

    def apply_theme(self, bundle: ThemeBundle) -> None:
        """Swap the active theme without restarting the host.

        Wired to ``/theme set`` via ``call_later(apply_theme, bundle)``
        so the mutation runs on Textual's own message pump (the
        slash-command handler lives on the host's asyncio task and
        cannot poke widget state directly).

        The agent log, app log, and input widget keep their state —
        only colours, TCSS, ASCII art, and status-bar display_name
        change. The widget topology is LOCKED, so ``show_*`` toggles
        flip ``.display`` instead of re-composing the tree; any theme
        that was rendering with a side pane can therefore hide it,
        and vice versa, without breaking the snapshot contract.
        """
        if (
            bundle.manifest.name == self._theme.manifest.name
            and bundle.path == self._theme.path
        ):
            return

        self._theme = bundle

        new_textual_theme = textual_theme_from_bundle(bundle)
        self.register_theme(new_textual_theme)

        key = self._css_source_key()
        self.stylesheet.add_source(
            self._compose_css(bundle),
            read_from=key,
            is_default_css=False,
        )

        self.theme = new_textual_theme.name

        self._apply_visibility(bundle)
        self._apply_art(bundle)
        self._apply_status_bar_text(bundle)

        self.refresh(layout=True)

    def _apply_visibility(self, bundle: ThemeBundle) -> None:
        """Honour ``show_*`` flags at runtime.

        Widgets were composed once at mount; we toggle ``display``
        rather than unmounting so a later theme with the pane enabled
        lights up again without a re-mount. Missing widgets (e.g. a
        theme that started with ``show_app_log=False`` never rendered
        ``#app-log``) are simply skipped — Textual raises
        :class:`NoMatches` and we catch it.
        """
        m = bundle.manifest
        for widget_id, visible in (
            ("#status-bar", m.show_status_bar),
            ("#app-log", m.show_app_log),
            ("#todo-pane", m.show_todo_pane),
        ):
            try:
                widget = self.query_one(widget_id)
            except Exception:
                continue
            widget.display = visible

        try:
            side_pane = self.query_one("#side-pane")
        except Exception:
            side_pane = None
        if side_pane is not None:
            side_pane.display = bool(
                m.show_app_log
                or m.show_todo_pane
                or (m.show_art and bool(bundle.art_frames))
            )

    def _apply_art(self, bundle: ThemeBundle) -> None:
        """Refresh ``#pipboy-art`` and resize side-pane for the new bundle."""
        m = bundle.manifest
        frames = bundle.art_frames if m.show_art else ()
        self._art_frames = frames
        self._art_frame_idx = 0

        art_h = min(30, bundle.art_frame_height + 2)
        try:
            art_widget = self.query_one("#pipboy-art", Static)
            art_widget.styles.height = art_h
            art_widget.update(
                self._center_art(frames[0], art_h) if frames else ""
            )
            art_widget.display = bool(frames)
        except Exception:
            pass


    def _apply_status_bar_text(self, bundle: ThemeBundle) -> None:
        """Reset the status bar's default text to the new theme's name.

        Overwritten the moment the next ``StatusMessage`` arrives; the
        reset matters for the idle gap between the theme swap and the
        next status event, when stale text would otherwise show.
        """
        try:
            status_bar = self.query_one("#status-bar", Static)
        except Exception:
            return
        status_bar.update(self._status_default_text())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_clear_log(self) -> None:
        """``Ctrl+L`` clears the agent log only — app log is preserved."""
        self._stream_buf = ""
        self._stream_tail_strips = 0
        self._think_buf = ""
        self._think_tail_strips = 0
        for widget_id in ("#agent-log", "#agent-log-detail"):
            try:
                self.query_one(widget_id, RichLog).clear()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Todo pane
    # ------------------------------------------------------------------

    def _apply_todo_write(self, tool_input: dict[str, object]) -> None:
        """Merge or replace ``_todos`` from a TodoWrite tool_input."""
        todos = tool_input.get("todos")
        if not isinstance(todos, list):
            return
        merge = bool(tool_input.get("merge", False))
        parsed: list[dict[str, str]] = []
        for item in todos:
            if not isinstance(item, dict):
                continue
            parsed.append({
                "id": str(item.get("id", "")),
                "content": str(item.get("content", "")),
                "status": str(item.get("status", "pending")),
            })
        if merge and self._todos:
            idx = {t["id"]: t for t in self._todos}
            for t in parsed:
                if t["id"] in idx:
                    idx[t["id"]].update(
                        {k: v for k, v in t.items() if v},
                    )
                else:
                    self._todos.append(t)
        else:
            self._todos = parsed
        self._refresh_todo_pane()

    def _render_todo_pane(self) -> str:
        """Format the current todo list for the ``#todo-pane`` Static widget."""
        if not self._todos:
            return ""
        _STATUS_GLYPH = {
            "completed": "[x]",
            "in_progress": "[~]",
            "cancelled": "[-]",
        }
        done = sum(1 for t in self._todos if t["status"] == "completed")
        lines: list[str] = [
            f"[bold]TODO[/]  {done}/{len(self._todos)}",
            "[dim]─────[/]",
        ]
        for t in self._todos:
            glyph = _STATUS_GLYPH.get(t["status"], "[ ]")
            text = t.get("content", "")
            if t["status"] == "completed":
                lines.append(f"[dim]{glyph} {text}[/]")
            elif t["status"] == "in_progress":
                lines.append(f"[bold bright_yellow]{glyph} {text}[/]")
            else:
                lines.append(f"{glyph} {text}")
        return "\n".join(lines)

    def _refresh_todo_pane(self) -> None:
        """Redraw ``#todo-pane`` from the current ``_todos`` list.

        Hidden when empty OR when every item is completed/cancelled —
        there's nothing actionable left to show.
        """
        try:
            widget = self.query_one("#todo-pane", Static)
        except Exception:
            return
        content = self._render_todo_pane()
        has_active = any(
            t["status"] not in ("completed", "cancelled")
            for t in self._todos
        )
        widget.update(content)
        show = bool(content) and has_active and self._theme.manifest.show_todo_pane
        widget.display = show

    # ------------------------------------------------------------------

    def _status_default_text(self) -> str:
        m = self._theme.manifest
        return f"Pip-Boy — theme: {m.display_name} v{m.version}"

    def _flush_stream_buffer(
        self, log_widget: RichLog, *, materialize_markdown: bool = False,
    ) -> None:
        """Drop streaming bookkeeping; optionally re-render the tail as Markdown.

        Trailing newlines in the buffer would render as a blank paragraph
        below the Markdown block, pushing the turn footer one line away
        from the reply. We ``rstrip`` so the footer sits flush against the
        last line of the assistant's text.
        """
        self._flush_think_buffer(log_widget)
        buf = self._stream_buf
        n = self._stream_tail_strips
        if materialize_markdown and buf.strip() and n > 0:
            _rich_log_strip_tail(log_widget, n)
            log_widget.write(Markdown(buf.rstrip(), justify="left"))
        self._stream_buf = ""
        self._stream_tail_strips = 0

    def _flush_think_buffer(self, log_widget: RichLog) -> None:  # noqa: ARG002
        self._think_buf = ""
        self._think_tail_strips = 0

    def _rewrite_think_tail(self, log_widget: RichLog) -> None:
        buf = self._think_buf
        if not buf:
            self._think_tail_strips = 0
            return
        _rich_log_strip_tail(log_widget, self._think_tail_strips)
        before = len(log_widget.lines)
        log_widget.write(Text("💭 " + buf.rstrip("\n"), style="dim italic"), expand=True)
        self._think_tail_strips = len(log_widget.lines) - before

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

    # ------------------------------------------------------------------
    # Clock & side-status rendering
    # ------------------------------------------------------------------

    def _center_art(self, frame: str, widget_height: int) -> str:
        """Return ``frame`` vertically centered within ``widget_height`` rows.

        Adds blank lines above and below so the art floats in the middle
        of the fixed-height ``#pipboy-art`` widget. Horizontal centering
        is handled by ``text-align: center`` in TCSS.
        """
        lines = frame.splitlines()
        top = (widget_height - len(lines)) // 2
        bottom = widget_height - len(lines) - top
        return "\n".join([""] * top + lines + [""] * bottom)

    def _tick_art(self) -> None:
        """Animation callback — advance to the next art frame."""
        if not self._art_frames:
            return
        self._art_frame_idx = (self._art_frame_idx + 1) % len(self._art_frames)
        try:
            widget = self.query_one("#pipboy-art", Static)
            art_h = widget.styles.height
            h = int(art_h.value) if art_h and art_h.value else self._theme.art_frame_height + 2
            widget.update(self._center_art(self._art_frames[self._art_frame_idx], h))
        except Exception:
            pass

    def _now(self) -> datetime:
        """Return the current time. Tests inject a frozen provider."""
        if self._clock_provider is not None:
            return self._clock_provider()
        return datetime.now()

    def _render_clock(self) -> str:
        """Return the clock panel — big date + time + weekday.

        Rendered via block glyphs so the HH:MM line reads as a large
        digital readout even on a 32-col budget. The weekday and full
        date sit above the numerals (``FRI  26 APR 2077`` style).
        """
        now = self._now()
        weekday = now.strftime("%a").upper()
        date_line = now.strftime("%d %b %Y").upper()
        time_line = now.strftime("%H:%M:%S")
        return f"{weekday}   {date_line}\n{time_line}"

    def _tick_clock(self) -> None:
        """``set_interval`` callback — redraw ``#pipboy-clock``."""
        try:
            widget = self.query_one("#pipboy-clock", Static)
        except Exception:
            return
        widget.update(self._render_clock())

    def _render_side_status(self) -> str:
        """Format the cached snapshot dict into the status panel body.

        Empty snapshot → a placeholder block so the TUI doesn't show a
        blank slab during the ~1s between mount and the first provider
        tick.

        Output uses Textual/Rich markup: ``[bold]LABEL[/]`` for field
        names, ``[dim]│[/]`` for the separator. The hosting ``Static``
        widget has ``markup=True`` by default.
        """
        s = self._side_snapshot
        if not s:
            return (
                "[bold]STATUS[/]\n"
                "[dim]─────[/]\n"
                "[dim]initializing…[/]"
            )

        # Ordered so the "what am I / where am I" fields come first and
        # the time-sensitive telemetry (reflect / cron / uptime) follow.
        ordered: tuple[tuple[str, str], ...] = (
            ("agent", "AGENT"),
            ("model", "MODEL"),
            ("chans", "CHANS"),
            ("memory", "MEMORY"),
            ("reflect", "REFLECT"),
            ("dream", "DREAM"),
            ("cron", "CRON"),
            ("uptime", "UPTIME"),
        )
        lines: list[str] = ["[bold]STATUS[/]", "[dim]─────[/]"]
        for key, label in ordered:
            value = s.get(key)
            if value is None or value == "":
                continue
            lines.append(f"[bold]{label:<7}[/] [dim]│[/] {value}")
        return "\n".join(lines)

    def _refresh_side_status(self) -> None:
        """Redraw ``#side-status`` from the cached snapshot."""
        try:
            widget = self.query_one("#side-status", Static)
        except Exception:
            return
        widget.update(self._render_side_status())

    def _tick_side_status(self) -> None:
        """``set_interval`` callback — pull fresh fields and repaint.

        The provider is best-effort: any exception is swallowed so a
        flaky data source (e.g. scheduler mid-restart) never crashes
        the TUI loop. An empty dict is a valid return — the cached
        snapshot keeps whatever it had.
        """
        if self._snapshot_provider is None:
            return
        try:
            fields = self._snapshot_provider()
        except Exception:
            return
        if not fields:
            return
        self._side_snapshot.update(fields)
        self._refresh_side_status()
