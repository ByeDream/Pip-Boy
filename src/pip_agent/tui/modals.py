"""Modal screens for interactive tool traces.

Two modals triggered from ``on_agent_message`` when the agent calls
specific built-in tools:

* :class:`AskUserModal`  — ``AskUserQuestion`` — shows the question
  text + option list, user picks (arrow keys / number hotkeys /
  Enter), the selection is dismissed as plain-text and forwarded
  to the chat via ``on_user_line``.

* :class:`PlanReviewModal` — ``ExitPlanMode`` — shows the plan body,
  user picks ``Approve`` / ``Request changes`` / ``Reject``. Selection
  becomes the next user turn so the agent decides what to do with
  it (there's no SDK permission round-trip — see
  ``OVERNIGHT_REPORT.md`` for why this path rather than
  ``can_use_tool``).

Both modals dismiss with ``None`` on Esc; the caller treats that
as "user cancelled" and injects nothing.

Cross-thread note: modals live in the Textual loop. We push them
from inside ``on_agent_message`` which is already on the Textual
thread (via the pump's ``post_message`` dispatch), so no
``call_from_thread`` / ``run_coroutine_threadsafe`` needed.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

__all__ = ["AskUserModal", "PlanReviewModal", "format_ask_answers"]


def format_ask_answers(answers: list[tuple[str, str]]) -> str:
    """Render a list of ``(header, label)`` pairs as a single chat line.

    Used by the App to compose the text it forwards via ``on_user_line``
    after :class:`AskUserModal` dismisses. Keeps all the selections on
    one user turn rather than spamming N turns for an N-question call.
    """
    if not answers:
        return ""
    if len(answers) == 1:
        header, label = answers[0]
        return f"{label}" if not header else f"{header}: {label}"
    parts = [
        (f"{hdr}: {lbl}" if hdr else lbl)
        for hdr, lbl in answers
    ]
    return " | ".join(parts)


class AskUserModal(ModalScreen[str | None]):
    """Collect answers for all questions in an AskUserQuestion tool call.

    Renders every question sequentially as an ``OptionList`` with its
    options + a synthetic "Other…" slot that opens a free-text input
    for custom answers. Submit collects all selections and dismisses
    with a single string formatted by :func:`format_ask_answers`.

    The modal works with a single Question just as well as with
    multiple — the iteration just terminates after Q1.
    """

    CSS = """
    AskUserModal {
        align: center middle;
    }
    #ask-modal-body {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #ask-modal-body > Label {
        margin-bottom: 1;
        text-style: bold;
    }
    #ask-modal-body OptionList {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }
    #ask-modal-body > Horizontal {
        height: 3;
        align: right middle;
    }
    #ask-modal-body Input {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("enter", "submit", "Submit", show=True, priority=False),
    ]

    def __init__(self, questions: list[dict[str, Any]]) -> None:
        super().__init__()
        # Filter out non-dict entries defensively; a malformed tool
        # input shouldn't crash the TUI.
        self._questions: list[dict[str, Any]] = [
            q for q in questions if isinstance(q, dict)
        ]
        self._selections: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="ask-modal-body"):
            yield Label(self._header_text())
            for idx, q in enumerate(self._questions, 1):
                yield Label(self._question_label(idx, q), classes="q-label")
                options_list: list[Option | str] = []
                options = q.get("options") or []
                if isinstance(options, list):
                    for opt_idx, opt in enumerate(options):
                        if not isinstance(opt, dict):
                            continue
                        label = str(opt.get("label", f"option {opt_idx}"))
                        desc = str(opt.get("description", ""))
                        display = f"{label}  —  {desc}" if desc else label
                        options_list.append(Option(display, id=f"q{idx}-o{opt_idx}"))
                options_list.append(Option("Other… (type below)",
                                           id=f"q{idx}-other"))
                yield OptionList(*options_list, id=f"q{idx}-list")
                yield Input(
                    placeholder="free-text answer (used if 'Other…' selected)",
                    id=f"q{idx}-input",
                )
            with Horizontal():
                yield Button("Submit", variant="primary", id="ask-submit")
                yield Button("Cancel", variant="default", id="ask-cancel")

    def _header_text(self) -> str:
        n = len(self._questions)
        if n == 1:
            return "Agent is asking you a question:"
        return f"Agent is asking you {n} questions:"

    def _question_label(self, idx: int, q: dict[str, Any]) -> str:
        header = str(q.get("header") or "")
        text = str(q.get("question") or "")
        multi = bool(q.get("multiSelect"))
        suffix = "  (multi-select)" if multi else ""
        if header:
            return f"Q{idx} [{header}]: {text}{suffix}"
        return f"Q{idx}: {text}{suffix}"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ask-submit":
            self.action_submit()
        elif event.button.id == "ask-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        """Collect each question's selection into a single chat line."""
        selections: list[tuple[str, str]] = []
        for idx, q in enumerate(self._questions, 1):
            try:
                lst = self.query_one(f"#q{idx}-list", OptionList)
            except Exception:
                continue
            highlighted = lst.highlighted
            if highlighted is None:
                continue  # user didn't touch this question — skip
            option = lst.get_option_at_index(highlighted)
            opt_id = option.id or ""
            header = str(q.get("header") or "")
            if opt_id.endswith("-other"):
                try:
                    custom = self.query_one(f"#q{idx}-input", Input).value
                except Exception:
                    custom = ""
                if not custom.strip():
                    continue  # "Other…" without text → treat as skipped
                selections.append((header, custom.strip()))
            else:
                options = q.get("options") or []
                opt_idx_s = opt_id.split("-o")[-1] if "-o" in opt_id else ""
                try:
                    opt_idx = int(opt_idx_s)
                except ValueError:
                    continue
                if opt_idx < len(options) and isinstance(options[opt_idx], dict):
                    label = str(options[opt_idx].get("label", ""))
                    selections.append((header, label))
        self.dismiss(format_ask_answers(selections) or None)


class PlanReviewModal(ModalScreen[str | None]):
    """Approval UX for the ``ExitPlanMode`` tool call.

    Shows the plan body in a scrollable block and three choice buttons.
    On select, dismisses with one of:

    * ``"approve"``                — user approves the plan as-is.
    * ``"request changes: <msg>"`` — user wants revisions, ``<msg>``
      is their free-text feedback.
    * ``"reject"``                 — user rejects the plan.
    * ``None``                     — user hit Esc.

    The caller forwards the dismissed string as the next user turn;
    the agent decides how to interpret it. No SDK permission round-
    trip is attempted (would require either can_use_tool or an MCP
    replacement — see OVERNIGHT_REPORT.md).
    """

    CSS = """
    PlanReviewModal {
        align: center middle;
    }
    #plan-modal-body {
        width: 90%;
        max-width: 120;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #plan-modal-body > Label {
        margin-bottom: 1;
        text-style: bold;
    }
    #plan-body {
        height: 1fr;
        border: round $accent-darken-2;
        padding: 0 1;
        background: $surface;
    }
    #plan-feedback {
        display: none;
        margin-top: 1;
    }
    #plan-feedback.visible {
        display: block;
    }
    #plan-modal-body > Horizontal {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("a", "approve", "Approve", show=True),
        Binding("r", "request_changes", "Request changes", show=True),
        Binding("x", "reject", "Reject", show=True),
    ]

    def __init__(self, plan_text: str) -> None:
        super().__init__()
        self._plan_text = plan_text or "(empty plan)"
        self._feedback_mode = False

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-modal-body"):
            yield Label("Agent is requesting approval for a plan:")
            with ScrollableContainer(id="plan-body"):
                yield Static(self._plan_text, markup=False)
            yield Input(
                placeholder="feedback for 'Request changes' (optional)",
                id="plan-feedback",
            )
            with Horizontal():
                yield Button("Approve (a)", variant="primary", id="plan-approve")
                yield Button("Request changes (r)", variant="warning",
                             id="plan-request")
                yield Button("Reject (x)", variant="error", id="plan-reject")
                yield Button("Cancel (esc)", variant="default", id="plan-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "plan-approve":
            self.action_approve()
        elif bid == "plan-request":
            self.action_request_changes()
        elif bid == "plan-reject":
            self.action_reject()
        elif bid == "plan-cancel":
            self.action_cancel()

    def action_approve(self) -> None:
        self.dismiss("approve")

    def action_reject(self) -> None:
        self.dismiss("reject")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_request_changes(self) -> None:
        """Two-step: reveal the feedback input, then submit on second press."""
        if not self._feedback_mode:
            self._feedback_mode = True
            try:
                feedback = self.query_one("#plan-feedback", Input)
                feedback.add_class("visible")
                feedback.focus()
            except Exception:
                pass
            return
        feedback_text = ""
        try:
            feedback_text = self.query_one("#plan-feedback", Input).value
        except Exception:
            pass
        feedback_text = feedback_text.strip()
        if feedback_text:
            self.dismiss(f"request changes: {feedback_text}")
        else:
            self.dismiss("request changes")
