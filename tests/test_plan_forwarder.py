"""Tests for :mod:`pip_agent.channels.plan_forwarder`.

Covers the ExitPlanMode interception contract used by remote channels
when the model exits plan mode: short plans ride inline (chunked by
``send_with_retry``), long plans go as a Markdown attachment, and the
other stream-event types (text_delta, thinking_delta, tool_use for
non-ExitPlanMode tools, finalize) are silently ignored so the forwarder
can compose with the WeCom progressive-reply renderer.
"""
from __future__ import annotations

from typing import Any

import pytest

from pip_agent.channels.plan_forwarder import (
    _APPROVAL_PROMPT,
    _INLINE_MAX_CHARS,
    PlanForwarder,
)


class _FakeChannel:
    """Records ``send`` / ``send_file`` calls; mimics the base :class:`Channel` surface."""

    name = "fake"

    def __init__(self, *, send_file_ok: bool = True, send_file_raises: bool = False) -> None:
        self.sends: list[tuple[str, str]] = []
        self.files: list[tuple[str, bytes, str, str]] = []
        self._send_file_ok = send_file_ok
        self._send_file_raises = send_file_raises

    @property
    def send_lock(self):
        import threading
        lk = getattr(self, "_lk", None)
        if lk is None:
            lk = threading.Lock()
            self._lk = lk
        return lk

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        self.sends.append((to, text))
        return True

    def send_file(
        self, to: str, file_data: bytes, filename: str = "",
        caption: str = "", **kwargs: Any,
    ) -> bool:
        if self._send_file_raises:
            raise RuntimeError("boom")
        self.files.append((to, file_data, filename, caption))
        return self._send_file_ok

    def release_inbound(self, inbound_id: str) -> None:
        return None


def _make(ch: _FakeChannel) -> PlanForwarder:
    return PlanForwarder(
        channel=ch,  # type: ignore[arg-type]
        to="peer1",
        account_id="acc1",
        inbound_id="inb1",
    )


# ---------------------------------------------------------------------------
# Non-ExitPlanMode events are silently ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ignores_text_and_thinking_deltas() -> None:
    ch = _FakeChannel()
    fwd = _make(ch)
    await fwd.handle_event("text_delta", text="hello")
    await fwd.handle_event("thinking_delta", text="pondering")
    await fwd.handle_event("finalize", final_text="done")
    assert ch.sends == []
    assert ch.files == []


@pytest.mark.asyncio
async def test_ignores_other_tool_uses() -> None:
    ch = _FakeChannel()
    fwd = _make(ch)
    await fwd.handle_event(
        "tool_use",
        name="Write",
        input={"file_path": "/tmp/a.md", "content": "x"},
    )
    await fwd.handle_event(
        "tool_use",
        name="EnterPlanMode",
        input={},
    )
    assert ch.sends == []
    assert ch.files == []


@pytest.mark.asyncio
async def test_ignores_empty_or_malformed_plan() -> None:
    ch = _FakeChannel()
    fwd = _make(ch)
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={})
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": ""})
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": "   \n\n  "})
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": 123})
    await fwd.handle_event("tool_use", name="ExitPlanMode", input=None)
    assert ch.sends == []
    assert ch.files == []


# ---------------------------------------------------------------------------
# Short plan → inline text with approval prompt appended
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_short_plan_sent_inline_with_approval_prompt() -> None:
    ch = _FakeChannel()
    fwd = _make(ch)
    plan = "1. do X\n2. do Y\n3. report back"
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": plan})
    assert len(ch.sends) == 1
    to, text = ch.sends[0]
    assert to == "peer1"
    assert plan in text
    assert _APPROVAL_PROMPT in text
    assert ch.files == []


@pytest.mark.asyncio
async def test_short_plan_strips_surrounding_whitespace() -> None:
    ch = _FakeChannel()
    fwd = _make(ch)
    plan_raw = "\n\n  Actual plan body  \n\n"
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": plan_raw})
    assert len(ch.sends) == 1
    assert ch.sends[0][1].startswith("Actual plan body")


# ---------------------------------------------------------------------------
# Long plan → Markdown attachment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_long_plan_sent_as_markdown_attachment() -> None:
    ch = _FakeChannel()
    fwd = _make(ch)
    plan = "x" * (_INLINE_MAX_CHARS + 1000)
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": plan})
    assert len(ch.files) == 1
    to, data, filename, caption = ch.files[0]
    assert to == "peer1"
    assert data == plan.encode("utf-8")
    assert filename.startswith("plan-") and filename.endswith(".md")
    assert "Markdown" in caption or "附件" in caption
    assert _APPROVAL_PROMPT in caption
    # No fallback inline send when send_file succeeds.
    assert ch.sends == []


@pytest.mark.asyncio
async def test_long_plan_falls_back_to_inline_when_send_file_fails() -> None:
    ch = _FakeChannel(send_file_ok=False)
    fwd = _make(ch)
    plan = "y" * (_INLINE_MAX_CHARS + 500)
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": plan})
    assert len(ch.files) == 1      # was attempted
    # send_with_retry chunks the inline text by channel limit, so the
    # send count depends on the fake's chunk policy (default 4096 chars).
    # We only care that *some* chunks landed and together cover the plan.
    assert ch.sends
    joined = "".join(text for _, text in ch.sends)
    assert plan in joined or plan[:1000] in joined  # body made it through
    assert _APPROVAL_PROMPT in joined


@pytest.mark.asyncio
async def test_long_plan_falls_back_when_send_file_raises() -> None:
    ch = _FakeChannel(send_file_raises=True)
    fwd = _make(ch)
    plan = "z" * (_INLINE_MAX_CHARS + 500)
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": plan})
    assert ch.files == []          # raised before recording
    assert ch.sends
    joined = "".join(text for _, text in ch.sends)
    assert _APPROVAL_PROMPT in joined


# ---------------------------------------------------------------------------
# Boundary: exactly _INLINE_MAX_CHARS still avoids the attachment path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boundary_exact_threshold_stays_inline() -> None:
    ch = _FakeChannel()
    fwd = _make(ch)
    plan = "a" * _INLINE_MAX_CHARS
    await fwd.handle_event("tool_use", name="ExitPlanMode", input={"plan": plan})
    assert ch.files == []    # did NOT use attachment path
    assert ch.sends          # something was sent inline (possibly chunked)


# ---------------------------------------------------------------------------
# Headless disable-list contract: only AskUserQuestion is stripped
# ---------------------------------------------------------------------------

def test_headless_only_disables_ask_user_question(monkeypatch: pytest.MonkeyPatch) -> None:
    import pip_agent.config as _cfg
    from pip_agent.agent_runner import _builtin_disallowed_tools
    from pip_agent.config import settings

    monkeypatch.setattr(_cfg, "headless", True)
    monkeypatch.setattr(settings, "use_custom_web_tools", False)
    disallowed = _builtin_disallowed_tools()
    assert "AskUserQuestion" in disallowed
    assert "TodoWrite" not in disallowed
    assert "EnterPlanMode" not in disallowed
    assert "ExitPlanMode" not in disallowed


def test_non_headless_keeps_all_interactive_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    import pip_agent.config as _cfg
    from pip_agent.agent_runner import _builtin_disallowed_tools
    from pip_agent.config import settings

    monkeypatch.setattr(_cfg, "headless", False)
    monkeypatch.setattr(settings, "use_custom_web_tools", False)
    disallowed = _builtin_disallowed_tools()
    assert disallowed == []


# ---------------------------------------------------------------------------
# Compose helper: fan events out, swallow per-sink exceptions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compose_stream_callbacks_fans_out() -> None:
    from pip_agent.agent_host import _compose_stream_callbacks

    a: list[tuple] = []
    b: list[tuple] = []

    async def cb_a(evt: str, **kw: Any) -> None:
        a.append((evt, kw))

    async def cb_b(evt: str, **kw: Any) -> None:
        b.append((evt, kw))

    composed = _compose_stream_callbacks(cb_a, cb_b)
    await composed("tool_use", name="X", input={})
    assert a == [("tool_use", {"name": "X", "input": {}})]
    assert b == [("tool_use", {"name": "X", "input": {}})]


@pytest.mark.asyncio
async def test_compose_stream_callbacks_swallows_exceptions() -> None:
    from pip_agent.agent_host import _compose_stream_callbacks

    b: list[str] = []

    async def cb_boom(evt: str, **kw: Any) -> None:
        raise RuntimeError("first sink is broken")

    async def cb_b(evt: str, **kw: Any) -> None:
        b.append(evt)

    composed = _compose_stream_callbacks(cb_boom, cb_b)
    await composed("text_delta", text="hi")  # must not raise
    assert b == ["text_delta"]


def test_compose_stream_callbacks_none_short_circuits() -> None:
    from pip_agent.agent_host import _compose_stream_callbacks

    assert _compose_stream_callbacks(None, None) is None


def test_compose_stream_callbacks_single_returns_identity() -> None:
    from pip_agent.agent_host import _compose_stream_callbacks

    async def cb(evt: str, **kw: Any) -> None:
        return None

    assert _compose_stream_callbacks(None, cb, None) is cb
