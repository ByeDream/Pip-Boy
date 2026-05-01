"""``Input`` subclass with arrow-key history and Tab-accept-suggestion.

The base :class:`textual.widgets.Input` is single-line and leaves the
``up``, ``down`` and ``tab`` keys unbound. We bind:

* ``up``   — move backwards through previously submitted lines, saving
  the in-progress draft on the first press so it can be restored.
* ``down`` — move forwards; once past the newest entry, restore the
  draft.
* ``tab``  — accept the inline suggestion (``Suggester`` ghost text)
  when the cursor sits at the end of the value AND a suggestion is
  showing; otherwise let focus move to the next widget.

Slash-command completion itself is delegated to Textual's built-in
:class:`textual.suggester.SuggestFromList` — pass it via ``suggester=``
the same way you would on a stock ``Input``. We don't ship our own
suggester here.

The history list lives in memory by default. Pass ``history_path`` to
persist to a UTF-8, newline-delimited file (last ``history_limit``
entries). Adjacent duplicates collapse so pressing Enter twice on the
same line stores one entry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from textual.binding import Binding
from textual.widgets import Input

log = logging.getLogger(__name__)

__all__ = ["HistoryInput"]


# Cap on retained entries. Large enough to span days of casual use,
# small enough that the file stays human-readable and parses instantly.
_DEFAULT_HISTORY_LIMIT = 500


class HistoryInput(Input):
    """``Input`` + arrow-key history navigation + Tab-accepts-suggestion.

    History is in-memory by default; pass ``history_path`` for a file
    that survives across sessions. Bindings are added on top of the
    parent's so all stock editing keys (left/right, ctrl+a/e, ...) keep
    working unchanged.
    """

    BINDINGS = [
        Binding("up", "history_prev", "Prev history", show=False),
        Binding("down", "history_next", "Next history", show=False),
        Binding("tab", "complete_or_focus_next", "Complete", show=False),
    ]

    def __init__(
        self,
        *args: Any,
        history_path: Path | None = None,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_path = history_path
        self._history_limit = max(1, int(history_limit))
        self._history_idx: int | None = None
        self._draft: str = ""
        if history_path is not None:
            self._load_history()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_history(self) -> None:
        path = self._history_path
        if path is None or not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("Failed to load history %s: %s", path, exc)
            return
        # Apply the same de-dup rule the runtime uses so a re-load
        # doesn't silently expand the in-memory list past the persisted
        # shape.
        cleaned: list[str] = []
        for line in lines:
            if line and (not cleaned or cleaned[-1] != line):
                cleaned.append(line)
        self._history = cleaned[-self._history_limit :]

    def _persist_history(self) -> None:
        path = self._history_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "\n".join(self._history[-self._history_limit :]),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Failed to persist history %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def action_submit(self) -> None:
        """Capture the value into history before forwarding the submit.

        ``Input.action_submit`` is a coroutine in Textual 1+, so we
        ``await`` super last. The post-processing runs even on empty
        / duplicate lines because the parent message must still post.
        """
        line = self.value
        if line.strip():
            if not self._history or self._history[-1] != line:
                self._history.append(line)
                if len(self._history) > self._history_limit:
                    overflow = len(self._history) - self._history_limit
                    del self._history[:overflow]
                self._persist_history()
        self._history_idx = None
        self._draft = ""
        await super().action_submit()

    def action_history_prev(self) -> None:
        """Move one entry backwards through history (saves draft once)."""
        if not self._history:
            return
        if self._history_idx is None:
            self._draft = self.value
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        else:
            return
        self._set_value(self._history[self._history_idx])

    def action_history_next(self) -> None:
        """Move one entry forwards; restore the draft past the newest."""
        if self._history_idx is None:
            return
        self._history_idx += 1
        if self._history_idx >= len(self._history):
            self._history_idx = None
            self._set_value(self._draft)
        else:
            self._set_value(self._history[self._history_idx])

    def action_complete_or_focus_next(self) -> None:
        """Tab: accept the suggestion when one is showing, else focus next.

        Mirrors the right-arrow accept path so users don't need to
        reach for the arrow key — Tab is the universal "complete this"
        gesture in shells. When no suggestion is active or the cursor
        isn't at end-of-value, fall back to ``focus_next`` so Tab still
        navigates between widgets.
        """
        suggestion = getattr(self, "_suggestion", "") or ""
        if (
            suggestion
            and self.cursor_at_end
            and suggestion != self.value
        ):
            self.value = suggestion
            self.cursor_position = len(self.value)
            return
        screen = getattr(self, "screen", None)
        if screen is not None:
            try:
                screen.focus_next()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_value(self, text: str) -> None:
        """Replace the input contents and park the cursor at the end."""
        self.value = text
        self.cursor_position = len(text)
