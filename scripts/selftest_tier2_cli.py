"""Tier 2 end-to-end self-test: three rapid-fire CLI messages fuse.

Fires three text-only messages into the CLI stdin back-to-back (no
waiting between them), so they land in the SAME inbound-drain tick.
Then asserts:

* At least one ``host.batch_coalesced`` event is emitted.
* After coalescing, exactly ONE ``stream.result`` event fires for the
  fused message (so the LLM only saw one turn).
* ``stream.opened`` fires exactly once (Tier 1 still in effect).

Note: because the inbound loop drains every 300 ms, there is an
inherent race between the OS scheduler and this test. We feed the
three lines as a single ``write()`` to maximise the odds of them
being read in one tick. If the machine is very slow, the three
messages may be split across ticks — in that case the assertion
degrades gracefully (we only *require* fuse>=1, not fuse==2).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(r"D:\Workspace\Pip-Boy")
TEST_WORKDIR = Path(r"D:\Workspace\pip-test")
PROFILE_DIR = TEST_WORKDIR / "profile-logs"
PROFILE_LOG = PROFILE_DIR / "profile.jsonl"
KEEP_LOG = PROFILE_DIR / "tier2_selftest.jsonl"
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
    # Tier 2 is on by default, but be explicit so a stale .env
    # override doesn't silently skip the test.
    env["BATCH_TEXT_INBOUNDS"] = "true"
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

    import threading

    def _reader() -> None:
        while True:
            line = proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                break
            stdout_log.append(line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    # Give the host time to boot.
    time.sleep(6.0)

    # Fire three messages in ONE write so they are as close as the OS
    # pipe scheduler allows. The inbound loop drains every 0.3 s; three
    # lines in one write almost always land in a single drain.
    print("[selftest] sending three lines in one write")
    payload = (
        "Please reply with exactly 'A'.\n"
        "Then reply with exactly 'B'.\n"
        "Finally reply with exactly 'C'. Answer all three in one message.\n"
    )
    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
    except BrokenPipeError:
        print("[selftest] pipe broke early")
        proc.kill()
        proc.wait()
        return 2

    # Wait long enough for the coalesced turn to complete, then /exit.
    # First turn still pays the CC handshake (~3-20 s depending on cache
    # warmth), so be generous.
    def _saw(tok: str, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if any(tok in line for line in stdout_log):
                return True
            time.sleep(0.2)
        return False

    _saw("C", 60.0)
    time.sleep(1.0)  # let stream.result flush

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

    batches = [e for e in events if _evt(e, "host.batch_coalesced")]
    opened = [e for e in events if _evt(e, "stream.opened")]
    results = [e for e in events if _evt(e, "stream.result")]

    print("\n===== Tier 2 event summary =====")
    print(f"  host.batch_coalesced : {len(batches)}  details={[_meta(b) for b in batches]}")
    print(f"  stream.opened        : {len(opened)}")
    print(f"  stream.result        : {len(results)}")

    ok = True
    total_fused = sum(_meta(b).get("fused", 0) for b in batches)
    ok &= _check(
        "T2.A at least one batch_coalesced event",
        len(batches) >= 1,
        f"got {len(batches)}",
    )
    ok &= _check(
        "T2.B total fused >= 1 (2 expected if all 3 landed together)",
        total_fused >= 1,
        f"fused={total_fused}",
    )
    ok &= _check(
        "T2.C single stream.opened (Tier 1 invariant)",
        len(opened) == 1,
        f"opened={len(opened)}",
    )
    # If all three fused, we expect ONE stream.result (single turn);
    # if only two fused, two results (one coalesced + one discrete).
    # Accept 1 or 2 — anything above means batching didn't trigger.
    ok &= _check(
        "T2.D stream.result count <= 2 (proves fusing happened)",
        len(results) <= 2,
        f"results={len(results)}",
    )

    print("\nOVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _check(label: str, cond: bool, detail: str) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}: {detail}")
    return cond


if __name__ == "__main__":
    raise SystemExit(main())
