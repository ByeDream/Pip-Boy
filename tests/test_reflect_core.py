"""Unit tests for the core ``reflect_from_jsonl`` function.

These cover contracts that the MCP wrapper (``test_mcp_reflect.py``) and
the PreCompact hook (``test_hooks.py``) rely on without exercising
directly. Locking them in here means callers get stable behaviour even
as the call sites diverge.

The design decisions under test are documented in
``docs/sdk-contract-notes.md`` §11.3:

* **Q1** — reflect output is capped at ≤5 observations.
* **Q7** — zero-byte-delta short-circuit: no LLM call when the cursor
  has not moved.
* **Q8** — failure-does-not-advance-cursor: LLM exceptions must leave
  ``start_offset`` pinned so the next trigger retries the same delta.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pip_agent.memory.reflect import (
    _MAX_OBSERVATIONS_PER_PASS,
    reflect_from_jsonl,
)


def _write_transcript(path: Path, lines: list[str]) -> None:
    """Write ``lines`` as a Claude-Code-style JSONL transcript."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _user_line(text: str) -> str:
    return (
        '{"type":"user","message":{"role":"user","content":' + f'"{text}"' + "}}"
    )


def _assistant_line(text: str) -> str:
    return (
        '{"type":"assistant","message":{"role":"assistant","content":'
        + f'"{text}"' + "}}"
    )


class TestQ7CursorGuard:
    """No LLM call when the cursor has not advanced."""

    def test_zero_new_bytes_returns_empty_without_llm(self, tmp_path):
        path = tmp_path / "sess.jsonl"
        _write_transcript(path, [_user_line("hi"), _assistant_line("hello")])
        full_size = path.stat().st_size

        class _NoCallClient:
            class messages:
                @staticmethod
                def create(*a, **kw):
                    raise AssertionError(
                        "LLM must not be called when cursor has zero delta"
                    )

        new_offset, obs = reflect_from_jsonl(
            path,
            start_offset=full_size,  # already past end of file
            agent_id="pip-boy",
            client=_NoCallClient(),
        )

        assert new_offset == full_size
        assert obs == []

    def test_back_to_back_calls_only_pay_llm_once(self, tmp_path):
        path = tmp_path / "sess.jsonl"
        _write_transcript(path, [_user_line("hi"), _assistant_line("hello")])

        call_count = {"n": 0}

        class _FakeResp:
            content = [type("B", (), {"text": "[]"})()]

        class _CountingClient:
            class messages:
                @staticmethod
                def create(*a, **kw):
                    call_count["n"] += 1
                    return _FakeResp()

        client = _CountingClient()

        # First pass: cursor at 0 → delta non-empty → LLM called once.
        offset_1, _ = reflect_from_jsonl(
            path, start_offset=0, agent_id="pip-boy", client=client,
        )
        # Second pass: cursor at end → zero delta → LLM MUST NOT be called.
        offset_2, obs_2 = reflect_from_jsonl(
            path, start_offset=offset_1, agent_id="pip-boy", client=client,
        )

        assert call_count["n"] == 1
        assert offset_2 == offset_1
        assert obs_2 == []


class TestQ8FailurePreservesCursor:
    """LLM exception must not advance the persisted cursor."""

    def test_llm_raise_leaves_start_offset_untouched(self, tmp_path):
        path = tmp_path / "sess.jsonl"
        _write_transcript(path, [_user_line("hi"), _assistant_line("hello")])

        class _FailingClient:
            class messages:
                @staticmethod
                def create(*a, **kw):
                    raise RuntimeError("simulated API outage")

        new_offset, obs = reflect_from_jsonl(
            path,
            start_offset=0,
            agent_id="pip-boy",
            client=_FailingClient(),
        )

        # Contract: returned offset == start_offset so the caller's
        # "persist only if offset advanced" logic preserves the cursor
        # and the next trigger re-reads the same delta.
        assert new_offset == 0
        assert obs == []

    def test_invalid_json_from_llm_advances_cursor_but_returns_empty(
        self, tmp_path,
    ):
        # Distinct contract: the LLM *responded*, just with garbage.
        # The delta has been reviewed, so the cursor moves forward;
        # we just can't extract anything. This is deliberately NOT
        # a Q8 case — Q8 is reserved for transport / API failures
        # that a retry could plausibly fix.
        path = tmp_path / "sess.jsonl"
        _write_transcript(path, [_user_line("hi"), _assistant_line("hello")])
        full_size = path.stat().st_size

        class _GarbageResp:
            content = [type("B", (), {"text": "not json at all"})()]

        class _GarbageClient:
            class messages:
                @staticmethod
                def create(*a, **kw):
                    return _GarbageResp()

        new_offset, obs = reflect_from_jsonl(
            path,
            start_offset=0,
            agent_id="pip-boy",
            client=_GarbageClient(),
        )

        assert new_offset == full_size
        assert obs == []


class TestQ1ObservationCap:
    """Hard cap on observations returned per reflect pass."""

    def test_cap_constant_matches_design_doc(self):
        assert _MAX_OBSERVATIONS_PER_PASS == 5

    def test_model_overrun_is_truncated_to_cap(self, tmp_path):
        # Model ignores the "≤5" prompt instruction and returns 12.
        # The defense-in-depth cap must truncate to 5 in insertion order.
        path = tmp_path / "sess.jsonl"
        _write_transcript(path, [_user_line("hi"), _assistant_line("hello")])

        dumped = (
            "["
            + ",".join(
                f'{{"text":"obs {i}","category":"decision"}}'
                for i in range(12)
            )
            + "]"
        )

        class _OverrunResp:
            content = [type("B", (), {"text": dumped})()]

        class _OverrunClient:
            class messages:
                @staticmethod
                def create(*a, **kw):
                    return _OverrunResp()

        _, obs = reflect_from_jsonl(
            path,
            start_offset=0,
            agent_id="pip-boy",
            client=_OverrunClient(),
        )

        assert len(obs) == 5
        # Preserve the model's own ordering (prompt framed it as
        # highest-signal first).
        assert [o["text"] for o in obs] == [f"obs {i}" for i in range(5)]

    def test_under_cap_is_unchanged(self, tmp_path):
        path = tmp_path / "sess.jsonl"
        _write_transcript(path, [_user_line("hi"), _assistant_line("hello")])

        dumped = (
            '[{"text":"only one","category":"decision"}]'
        )

        class _Resp:
            content = [type("B", (), {"text": dumped})()]

        class _Client:
            class messages:
                @staticmethod
                def create(*a, **kw):
                    return _Resp()

        _, obs = reflect_from_jsonl(
            path,
            start_offset=0,
            agent_id="pip-boy",
            client=_Client(),
        )

        assert len(obs) == 1
        assert obs[0]["text"] == "only one"


class TestPromptPreconditions:
    """Cheap short-circuits that shouldn't even reach the client."""

    def test_missing_file_returns_start_offset(self, tmp_path):
        missing = tmp_path / "does-not-exist.jsonl"

        class _BoomClient:
            class messages:
                @staticmethod
                def create(*a, **kw):
                    raise AssertionError(
                        "LLM must not be called for missing transcript"
                    )

        new_offset, obs = reflect_from_jsonl(
            missing,
            start_offset=42,
            agent_id="pip-boy",
            client=_BoomClient(),
        )

        assert new_offset == 42
        assert obs == []

    def test_no_client_and_no_env_returns_empty(self, tmp_path):
        path = tmp_path / "sess.jsonl"
        _write_transcript(path, [_user_line("hi")])

        with patch(
            "pip_agent.memory.reflect.build_anthropic_client",
            return_value=None,
        ):
            new_offset, obs = reflect_from_jsonl(
                path,
                start_offset=0,
                agent_id="pip-boy",
                client=None,
            )

        assert new_offset == 0
        assert obs == []
