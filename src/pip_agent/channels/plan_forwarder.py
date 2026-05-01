"""Forward ``ExitPlanMode`` plans to remote-channel peers.

Plan mode's UX contract — "show the plan, let the user approve / request
changes / reject" — maps onto chat transports without the SDK
permission round-trip. Claude calls ``ExitPlanMode(plan=...)``; we send
the plan body to the user's conversation; their next message becomes the
approval reply in the agent's transcript on its own, with no channel-side
state machine. The agent history itself encodes "waiting on approval",
so if the user answers "today the weather is nice" instead of yes/no the
model gets to disambiguate — exactly as it already does on CLI via
``PlanReviewModal``.

Used for WeCom and WeChat. CLI has its own modal path
(``pip_agent.tui.modals.PlanReviewModal``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pip_agent.channels.base import Channel

log = logging.getLogger(__name__)


# Plans up to this many chars ride inline — the per-channel chunker in
# ``send_with_retry`` will split across multiple messages if needed.
# Past this threshold we switch to a Markdown attachment: at 8000+ chars
# even WeCom's 4096-char chunking produces three-plus bubbles, and file
# attachments are easier to reread / keep than chat scrollback.
_INLINE_MAX_CHARS = 8000

_APPROVAL_PROMPT = "回复 同意 / 修改意见 / 拒绝 来决定是否推进。"


class PlanForwarder:
    """``on_stream_event`` sink that forwards ``ExitPlanMode`` plans.

    Reacts only to ``tool_use`` events whose ``name == "ExitPlanMode"``.
    Silently ignores every other event type, so it composes with the
    WeCom streaming renderer via
    :func:`pip_agent.agent_host._compose_stream_callbacks`.
    """

    def __init__(
        self,
        *,
        channel: "Channel",
        to: str,
        account_id: str = "",
        inbound_id: str = "",
    ) -> None:
        self._channel = channel
        self._to = to
        self._account_id = account_id
        self._inbound_id = inbound_id

    async def handle_event(self, event_type: str, **kwargs: Any) -> None:
        if event_type != "tool_use":
            return
        if kwargs.get("name") != "ExitPlanMode":
            return
        tool_input = kwargs.get("input")
        plan = (
            tool_input.get("plan")
            if isinstance(tool_input, dict)
            else None
        )
        if not isinstance(plan, str):
            return
        plan = plan.strip()
        if not plan:
            return
        try:
            await asyncio.to_thread(self._dispatch, plan)
        except Exception:
            log.exception(
                "plan_forwarder: dispatch raised on %s", self._channel.name,
            )

    def _dispatch(self, plan: str) -> None:
        from pip_agent.channels.base import send_with_retry

        if len(plan) <= _INLINE_MAX_CHARS:
            send_with_retry(
                self._channel,
                self._to,
                f"{plan}\n\n---\n{_APPROVAL_PROMPT}",
                inbound_id=self._inbound_id,
                account_id=self._account_id,
            )
            return

        filename = f"plan-{int(time.time())}.md"
        caption = (
            f"计划较长({len(plan)} 字),已作为 Markdown 附件发送。"
            f"{_APPROVAL_PROMPT}"
        )
        ok = False
        try:
            ok = self._channel.send_file(
                self._to,
                plan.encode("utf-8"),
                filename=filename,
                caption=caption,
                account_id=self._account_id,
            )
        except Exception:
            log.exception(
                "plan_forwarder: send_file raised on %s; falling back "
                "to chunked inline text.", self._channel.name,
            )
        if not ok:
            send_with_retry(
                self._channel,
                self._to,
                f"{plan}\n\n---\n{_APPROVAL_PROMPT}",
                inbound_id=self._inbound_id,
                account_id=self._account_id,
            )
