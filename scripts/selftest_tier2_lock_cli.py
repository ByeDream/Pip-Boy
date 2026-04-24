"""Tier 2 lock-time coalescing self-test: rapid-fire messages while a turn runs.

Distinct from ``selftest_tier2_cli.py``:

* ``selftest_tier2_cli.py`` exercises the **drain-time** path: three
  messages land in a single inbound-queue drain tick and fuse before
  dispatch.
* This test exercises the **lock-time** path: fire ``msg1``, wait long
  enough for its turn to have claimed the session lock AND be in the
  middle of ``session.connect()`` (the first CC handshake costs
  hundreds of ms even on a warm box), then fire ``msg2`` and ``msg3``.
  They land in separate drain ticks, so drain-time can't fuse them —
  but the ``_execute_turn`` gate should redirect them into
  ``_pending_per_session[sk]``, and ``_run_turn_streaming``'s
  lock-time pop should drain + fuse them into msg1's turn.

Expected profile shape:

* ``host.redirected_to_lock_batch`` fires for msg2 and msg3 (2+).
* One ``host.batch_coalesced`` event with ``source="lock_time"`` and
  ``fused >= 2`` OR a ``source="leftover_flush"`` event (if msg1
  already passed lock-time before msg2/3 arrived — the test still
  proves the coalescing wiring works, just via the flush path).
* ``stream.result`` count is 1 or 2 (1 = pure lock-time fusion;
  2 = msg1 ran alone + one follow-up merged turn).

If the test is fast enough that the drain-time path kicks in instead
(all three messages land in one tick), the assertions degrade to
"at least one batch_coalesced event fired" — batching worked, just
through a different code path.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(r"D:\Workspace\Pip-Boy")
TEST_WORKDIR = Path(r"D:\Workspace\pip-test")
PROFILE_DIR = TEST_WORKDIR / "profile-logs"
PROFILE_LOG = PROFILE_DIR / "profile.jsonl"
KEEP_LOG = PROFILE_DIR / "tier2_lock_selftest.jsonl"
PYTHON_EXE = TEST_WORKDIR / ".venv" / "Scripts" / "python.exe"

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True,
    )


def main() -> int:
    if PROFILE_LOG.exists():
        PROFILE_LOG.unlink()

    env = os.environ.copy()
    env["ENABLE_PROFILER"] = "true"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["BATCH_TEXT_INBOUNDS"] = "true"
    env["ENABLE_STREAMING_SESSION"] = "true"
    # Keep this self-test CLI-only: channel enablement is on-demand
    # (README "Channel enablement rules"), so scrub messaging envs
    # that a local .env might have injected.
    env.pop("WECOM_BOT_ID", None)
    env.pop("WECOM_BOT_SECRET", None)

    proc = subprocess.Popen(
        [str(PYTHON_EXE), "-m", "pip_agent"],
        cwd=str(TEST_WORKDIR),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdin is not None and proc.stdout is not None

    stdout_log: list[str] = []

    def _reader() -> None:
        while True:
            line = proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                break
            stdout_log.append(line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    # Let the host boot fully (channels_ready, idle_sweep started).
    time.sleep(6.0)

    # msg1: fires first, forces fresh stream.connect (expensive, gives
    # us a wide window for msg2/msg3 to land while session is still
    # warming up).
    print("[selftest] firing msg1 alone")
    try:
        proc.stdin.write("Please reply with exactly the single letter X.\n")
        proc.stdin.flush()
    except BrokenPipeError:
        print("[selftest] pipe broke before msg1")
        proc.kill()
        proc.wait()
        return 2

    # Wait long enough for msg1 to be drained + claim _session_active,
    # but ideally NOT long enough for its run_turn to have started
    # (the Tier 1 first-turn handshake is ~600-1500 ms on Windows).
    # 0.5 s is usually in the sweet spot; a fast machine might already
    # be past lock-time pop — in that case msg2/3 go via
    # leftover_flush, which the assertions tolerate.
    time.sleep(0.5)

    print("[selftest] firing msg2")
    try:
        proc.stdin.write("Now reply with exactly the single letter Y.\n")
        proc.stdin.flush()
    except BrokenPipeError:
        print("[selftest] pipe broke before msg2")
        proc.kill()
        proc.wait()
        return 2

    time.sleep(0.35)  # separate drain tick vs msg2

    print("[selftest] firing msg3")
    try:
        proc.stdin.write("And finally reply with exactly the single letter Z.\n")
        proc.stdin.flush()
    except BrokenPipeError:
        print("[selftest] pipe broke before msg3")
        proc.kill()
        proc.wait()
        return 2

    # Wait for all turns to complete. If lock-time fusion kicks in,
    # we see one turn answering X/Y/Z together. If leftover_flush
    # kicks in, we see two turns (msg1 alone, then msg2+msg3 merged).
    # Either way "Z" should appear in some reply within 60 s.
    def _saw(tok: str, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if any(tok in line for line in stdout_log):
                return True
            time.sleep(0.2)
        return False

    _saw("Z", 90.0)
    time.sleep(1.5)  # let stream.result + release/flush settle

    try:
        proc.stdin.write("/exit\n")
        proc.stdin.flush()
    except BrokenPipeError:
        pass

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    tail = "".join(stdout_log)[-3000:]
    print("\n===== HOST STDOUT (tail) =====")
    try:
        sys.stdout.write(tail)
        sys.stdout.write("\n")
    except Exception:
        pass

    if not PROFILE_LOG.exists():
        print("[selftest][FAIL] profile log not written")
        return 2

    KEEP_LOG.write_bytes(PROFILE_LOG.read_bytes())

    events: list[dict] = []
    with PROFILE_LOG.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _evt(e: dict, name: str) -> bool:
        return e.get("evt") == name

    def _meta(e: dict) -> dict:
        m = e.get("meta")
        return m if isinstance(m, dict) else {}

    redirected = [e for e in events if _evt(e, "host.redirected_to_lock_batch")]
    batches = [e for e in events if _evt(e, "host.batch_coalesced")]
    opened = [e for e in events if _evt(e, "stream.opened")]
    results = [e for e in events if _evt(e, "stream.result")]

    sources = [_meta(b).get("source", "?") for b in batches]
    total_fused = sum(_meta(b).get("fused", 0) for b in batches)

    print("\n===== Tier 2 lock-time event summary =====")
    print(f"  redirected_to_lock_batch : {len(redirected)}")
    print(f"  host.batch_coalesced     : {len(batches)} sources={sources}")
    print(f"  stream.opened            : {len(opened)}")
    print(f"  stream.result            : {len(results)}")
    print(f"  total fused              : {total_fused}")
    print(f"  batch detail             : {[_meta(b) for b in batches]}")

    ok = True
    # Core contract: batching happened via SOME path (lock-time,
    # leftover-flush, or drain-time fallback).
    ok &= _check(
        "T2L.A at least one batch_coalesced event fired",
        len(batches) >= 1,
        f"got {len(batches)}",
    )
    # Stronger: at least one batch came from a lock-time-era source.
    # Accept either lock_time (ideal) or leftover_flush (still proves
    # the new wiring works; just a tighter race than expected).
    lock_era = [
        b for b in batches
        if _meta(b).get("source") in ("lock_time", "leftover_flush")
    ]
    ok &= _check(
        "T2L.B at least one lock_time or leftover_flush batch",
        len(lock_era) >= 1,
        f"lock_era_sources={[_meta(b).get('source') for b in lock_era]}",
    )
    # Bound on stream.result count proves we didn't run 3 separate
    # turns. 1 = perfect lock-time fusion. 2 = msg1 alone + merged
    # follow-up. Anything >2 means batching failed entirely.
    ok &= _check(
        "T2L.C stream.result count <= 2 (fusion actually reduced turns)",
        len(results) <= 2,
        f"results={len(results)}",
    )
    # Session reuse: we expect exactly one stream.opened (Tier 1 still
    # works under the new gate).
    ok &= _check(
        "T2L.D exactly one stream.opened (Tier 1 invariant preserved)",
        len(opened) == 1,
        f"opened={len(opened)}",
    )
    # Total fused count should account for at least one merge (>=1).
    ok &= _check(
        "T2L.E total fused >= 1",
        total_fused >= 1,
        f"total_fused={total_fused}",
    )

    print("\nOVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _check(label: str, cond: bool, detail: str) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}: {detail}")
    return cond


if __name__ == "__main__":
    raise SystemExit(main())
