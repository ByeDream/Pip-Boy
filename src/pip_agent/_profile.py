"""Lightweight production profiler.

Emits structured JSONL timing records (``span`` / ``event`` / ``turn``) for
every request flowing through Pip-Boy. Designed to stay in the codebase
permanently, gated off by default.

How to turn it on
-----------------

Either set ``ENABLE_PROFILER=true`` in ``.env`` (the canonical path) or
export ``PIP_PROFILE=1`` in the shell for a one-off session. Output goes
to ``profile_dir`` (default ``D:\\Workspace\\pip-test\\profile-logs``) and
the file name is fixed at ``profile.jsonl`` — rename it between scenarios
if you're doing comparative runs.

Design notes
------------
* Zero new dependencies — stdlib only.
* Default state is no-op: unless profiling is enabled the ``span`` /
  ``event`` / ``new_turn`` calls all short-circuit. Hot-path cost when
  disabled is a single attribute read and a branch.
* Enabled state writes JSONL, one event per line, append-only, under a
  single ``threading.Lock``. Line-buffered ``open`` guarantees each JSON
  line is flushed atomically — essential because wecom WS, wechat long-poll,
  and the asyncio host thread all emit concurrently. Without the lock we'd
  see half-written lines interleave.
* ``contextvars.ContextVar`` carries the current ``turn_id`` / ``channel`` /
  ``sender`` / span stack across ``async with`` boundaries. Multiple
  ``process_inbound`` coroutines running in parallel each have their own
  context — their spans don't pollute each other.
* Sync code (thread entry points like wecom's ``_on_message``, wechat
  ``poll``) uses ``span_sync`` backed by ``threading.local`` so
  non-asyncio threads still get a span stack. Falls back to the async
  contextvar parent when called from inside an ``async with span(...)``
  block.
* Every record carries ``turn_id``, ``tid``, ``task_id`` (when in a
  coroutine), ``span_id``, and ``parent_span_id`` — concurrent-channel
  analysis groups by ``turn_id`` and reconstructs parallel timelines from
  those.

Cold-start tracking
-------------------

``PROCESS_START_MONO_NS`` is captured at module import, which happens
very early in ``__main__.main`` (via ``bootstrap()``). ``cold_start()``
emits a point-in-time event that records the wall time elapsed since
that anchor, so downstream analysis can reconstruct the full "python
process up → host ready for first inbound" timeline from the log alone.
"""

from __future__ import annotations

import atexit
import contextvars
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

# Captured at import. Module import happens inside ``__main__.main`` before
# the host starts, so this is the closest we can get to "python process start"
# without touching ``sys.executable`` launcher time. Every ``cold_start(...)``
# event records the delta from this anchor.
PROCESS_START_MONO_NS: int = time.perf_counter_ns()

_ENABLED: bool = False
_FILE: Any = None
_LOCK = threading.Lock()
_SEQ = 0
_START_MONO_NS: int = 0
_COUNTS: dict[str, int] = {}

_CURRENT_TURN: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "pip_profile_current_turn", default=None
)
_CURRENT_SPAN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pip_profile_current_span_id", default=None
)

_SYNC_LOCAL = threading.local()


def _sync_stack() -> list[str]:
    stack = getattr(_SYNC_LOCAL, "span_stack", None)
    if stack is None:
        stack = []
        _SYNC_LOCAL.span_stack = stack
    return stack


def _current_task_id() -> int | None:
    try:
        import asyncio

        task = asyncio.current_task()
    except Exception:
        return None
    return id(task) if task is not None else None


def _next_seq() -> int:
    global _SEQ
    with _LOCK:
        _SEQ += 1
        return _SEQ


def _write(record: dict[str, Any]) -> None:
    if not _ENABLED or _FILE is None:
        return
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
    except Exception as exc:
        line = json.dumps({"evt": "_encode_error", "err": str(exc)[:200]})
    with _LOCK:
        try:
            _FILE.write(line + "\n")
        except Exception:
            pass
        _COUNTS[record.get("evt", "?")] = _COUNTS.get(record.get("evt", "?"), 0) + 1


def _base_record(evt: str, meta: dict[str, Any]) -> dict[str, Any]:
    turn = _CURRENT_TURN.get()
    rec: dict[str, Any] = {
        "ts": time.time(),
        "mono_ns": time.perf_counter_ns(),
        "seq": _next_seq(),
        "pid": os.getpid(),
        "tid": threading.get_ident(),
        "task_id": _current_task_id(),
        "turn_id": turn["turn_id"] if turn else None,
        "channel": meta.pop("channel", None) or (turn.get("channel") if turn else None),
        "evt": evt,
    }
    if meta:
        rec["meta"] = meta
    return rec


def enabled() -> bool:
    """Fast check used by hot-path shortcuts."""
    return _ENABLED


def event(name: str, /, **meta: Any) -> None:
    """Record a point-in-time event with optional metadata.

    ``name`` is positional-only so callers can still pass ``name=`` as
    metadata (e.g. ``event("runner.tool_use", name=block.name)``) without
    colliding with the parameter.
    """
    if not _ENABLED:
        return
    rec = _base_record(name, dict(meta))
    parent = _CURRENT_SPAN_ID.get()
    if parent is not None:
        rec["parent_span_id"] = parent
    _write(rec)


def cold_start(milestone: str, /, **meta: Any) -> None:
    """Record a cold-start milestone relative to process import time.

    Use for startup phases that happen before the first ``process_inbound``
    call (``logging_ready``, ``run_host_entered``, ``channels_ready``,
    ``scheduler_ready``, ``loop_ready``, etc.). The ``since_start_ms``
    field lets a reader reconstruct the full startup timeline without
    needing a separate run.

    No-op when profiling is disabled, same as every other emission.
    """
    if not _ENABLED:
        return
    now_ns = time.perf_counter_ns()
    payload = dict(meta)
    payload["since_start_ms"] = round(
        (now_ns - PROCESS_START_MONO_NS) / 1_000_000.0, 3,
    )
    event(f"cold_start.{milestone}", **payload)


def new_turn(channel: str | None = None, sender: str | None = None, **meta: Any) -> str:
    """Open a new logical turn; assign ``turn_id`` for all downstream spans.

    Call at the top of ``AgentHost.process_inbound``. Returns the turn_id
    so the caller can log it if useful.
    """
    turn_id = uuid.uuid4().hex[:12]
    if _ENABLED:
        _CURRENT_TURN.set(
            {
                "turn_id": turn_id,
                "channel": channel,
                "sender": sender,
            }
        )
        rec_meta: dict[str, Any] = {"sender": sender}
        if channel is not None:
            rec_meta["channel"] = channel
        rec_meta.update(meta)
        event("turn.open", **rec_meta)
    return turn_id


def end_turn(**meta: Any) -> None:
    """Mark the end of the current turn."""
    if not _ENABLED:
        return
    event("turn.close", **meta)
    _CURRENT_TURN.set(None)


@asynccontextmanager
async def span(name: str, /, **meta: Any):
    """Async span, bound to the current coroutine via contextvars.

    Emits ``span.open`` on enter and ``span.close`` on exit with ``dur_ms``.
    Two coroutines running in parallel each have their own span stack thanks
    to ``contextvars``.
    """
    if not _ENABLED:
        yield
        return

    span_id = uuid.uuid4().hex[:8]
    parent_id = _CURRENT_SPAN_ID.get()
    token = _CURRENT_SPAN_ID.set(span_id)

    start_ns = time.perf_counter_ns()
    open_rec = _base_record("span.open", dict(meta))
    open_rec["name"] = name
    open_rec["span_id"] = span_id
    if parent_id is not None:
        open_rec["parent_span_id"] = parent_id
    _write(open_rec)

    err: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        err = exc
        raise
    finally:
        dur_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        close_rec = _base_record("span.close", {})
        close_rec["name"] = name
        close_rec["span_id"] = span_id
        close_rec["dur_ms"] = round(dur_ms, 3)
        if parent_id is not None:
            close_rec["parent_span_id"] = parent_id
        if err is not None:
            close_rec["err"] = type(err).__name__
        _write(close_rec)
        _CURRENT_SPAN_ID.reset(token)


@contextmanager
def span_sync(name: str, /, **meta: Any):
    """Sync span for non-asyncio threads (wecom WS handler, wechat poll thread).

    Uses ``threading.local`` for the span stack because ``contextvars`` in
    a raw thread is not as useful — it doesn't propagate the way it does
    across ``await``. Same on-disk shape as ``span``.
    """
    if not _ENABLED:
        yield
        return

    stack = _sync_stack()
    span_id = uuid.uuid4().hex[:8]
    # Parent resolution order: (a) a sync span already open on this thread,
    # (b) an async span open in the current contextvars context (``_prepare_turn``
    # called from inside an ``async with span(...)`` block is the canonical case),
    # (c) no parent. Without (b) a sync sub-span called from async code would
    # orphan itself and break turn reconstruction.
    parent_id = stack[-1] if stack else _CURRENT_SPAN_ID.get()
    stack.append(span_id)

    start_ns = time.perf_counter_ns()
    open_rec = _base_record("span.open", dict(meta))
    open_rec["name"] = name
    open_rec["span_id"] = span_id
    if parent_id is not None:
        open_rec["parent_span_id"] = parent_id
    _write(open_rec)

    err: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        err = exc
        raise
    finally:
        dur_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        close_rec = _base_record("span.close", {})
        close_rec["name"] = name
        close_rec["span_id"] = span_id
        close_rec["dur_ms"] = round(dur_ms, 3)
        if parent_id is not None:
            close_rec["parent_span_id"] = parent_id
        if err is not None:
            close_rec["err"] = type(err).__name__
        _write(close_rec)
        if stack and stack[-1] == span_id:
            stack.pop()


def _flush_summary() -> None:
    """atexit hook: write a ``_summary`` record so the tail of the file has
    a quick-scan health line.
    """
    if not _ENABLED or _FILE is None:
        return
    try:
        wall_ms = (time.perf_counter_ns() - _START_MONO_NS) / 1_000_000.0
        rec = {
            "ts": time.time(),
            "mono_ns": time.perf_counter_ns(),
            "evt": "_summary",
            "wall_ms": round(wall_ms, 3),
            "counts": dict(_COUNTS),
        }
        with _LOCK:
            _FILE.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _FILE.flush()
            try:
                _FILE.close()
            except Exception:
                pass
    except Exception:
        pass


def _resolve_enable_flag() -> bool:
    """Read the enable flag from settings first, then fall back to env vars.

    Settings-first so ``.env`` is the canonical control surface. Env vars
    remain a valid one-off override — handy for ``$env:PIP_PROFILE=1`` in
    a shell without editing ``.env``.
    """
    try:
        from pip_agent.config import settings

        if getattr(settings, "enable_profiler", False):
            return True
    except Exception:
        # Settings module might not be importable during very early
        # bootstrap (e.g. from a test harness that stubs config out).
        # Fall through to env vars silently.
        pass

    flag = os.environ.get("PIP_PROFILE", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _resolve_output_dir() -> Path:
    """Choose the profile output directory: settings → env var → default."""
    try:
        from pip_agent.config import settings

        cfg = getattr(settings, "profile_dir", "") or ""
        if cfg:
            return Path(cfg)
    except Exception:
        pass

    from_env = os.environ.get("PIP_PROFILE_DIR", "").strip()
    if from_env:
        return Path(from_env)
    return Path(r"D:\Workspace\pip-test\profile-logs")


def bootstrap() -> None:
    """Enable profiling if the config flag (or the ``PIP_PROFILE`` env var)
    is set.

    Must be called early in ``__main__.main`` (before any instrumented
    module starts emitting). Safe to call multiple times; subsequent
    calls are no-ops once a file is open.
    """
    global _ENABLED, _FILE, _START_MONO_NS

    if _ENABLED:
        return
    if not _resolve_enable_flag():
        return

    base = _resolve_output_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        base = Path(".")

    path = base / "profile.jsonl"
    try:
        f = open(path, "a", buffering=1, encoding="utf-8")
    except Exception:
        return

    _FILE = f
    _ENABLED = True
    _START_MONO_NS = time.perf_counter_ns()
    atexit.register(_flush_summary)

    _write(
        {
            "ts": time.time(),
            "mono_ns": _START_MONO_NS,
            "seq": _next_seq(),
            "pid": os.getpid(),
            "tid": threading.get_ident(),
            "evt": "_bootstrap",
            "meta": {
                "path": str(path),
                # How much elapsed between ``import _profile`` and
                # ``bootstrap()`` being called — mostly Python startup +
                # imports. Useful for separating "interpreter warm-up"
                # from "host scaffolding" in the cold-start timeline.
                "since_start_ms": round(
                    (_START_MONO_NS - PROCESS_START_MONO_NS) / 1_000_000.0, 3,
                ),
            },
        }
    )
