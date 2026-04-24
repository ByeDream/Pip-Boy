"""Tier 1 self-verification harness.

Launches Pip-Boy in CLI-only mode, feeds 3 short user messages, then
``/exit``. Tails the profile JSONL afterwards and asserts the Tier 1
contract:

* Exactly one ``stream.opened`` for the session (first turn only).
* Every turn emits ``stream.session_init`` with small ``since_stream_ms``
  (>> smaller than a real subprocess spawn; threshold 50 ms).
* Turns 2+ emit ``stream.reused`` (cache hit).
* All three ``stream.result`` events share the same ``session_key`` —
  confirms the cached client served all three turns.

Run::

    D:\\Workspace\\pip-test\\.venv\\Scripts\\python.exe \\
        D:\\Workspace\\Pip-Boy\\scripts\\selftest_tier1_cli.py

Writes ``D:/Workspace/pip-test/profile-logs/tier1_selftest.jsonl``.
"""
from __future__ import annotations

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
KEEP_LOG = PROFILE_DIR / "tier1_selftest.jsonl"
PYTHON_EXE = TEST_WORKDIR / ".venv" / "Scripts" / "python.exe"


def main() -> int:
    # ``--offline`` skips the (expensive) host run and re-analyses the
    # last saved ``tier1_selftest.jsonl``. Handy while iterating on the
    # gate checks after a real run has already happened.
    offline = "--offline" in sys.argv
    if offline:
        if not KEEP_LOG.exists():
            print(f"[selftest] --offline: {KEEP_LOG} missing")
            return 2
        print(f"[selftest] --offline: re-analysing {KEEP_LOG}")
        return _analyse(KEEP_LOG)

    if PROFILE_LOG.exists():
        PROFILE_LOG.unlink()

    env = os.environ.copy()
    env["ENABLE_PROFILER"] = "true"
    # Force UTF-8 for child stdout so log lines containing non-ASCII
    # (e.g. the arrow in ``offset=a\u2192b``) don't crash the cp1252
    # StreamHandler under Windows Python 3.14.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # Rely on .env in the CWD for credentials.

    messages = [
        "Please answer 'ack1' and nothing else.",
        "Please answer 'ack2' and nothing else.",
        "Please answer 'ack3' and nothing else.",
    ]

    # Keep this self-test CLI-only: the new host picks channels on
    # demand (see README "Channel enablement rules"), so we scrub the
    # messaging envs to prevent an ambient .env from firing up WeCom
    # / WeChat during what's supposed to be a CLI latency probe.
    env.pop("WECOM_BOT_ID", None)
    env.pop("WECOM_BOT_SECRET", None)

    proc = subprocess.Popen(
        [
            str(PYTHON_EXE),
            "-m", "pip_agent",
        ],
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

    # Give the host time to boot before the first prompt.
    time.sleep(6.0)

    stdout_log: list[str] = []

    def _drain_stdout(timeout_s: float) -> None:
        end = time.time() + timeout_s
        while time.time() < end:
            # Read what's available without blocking on newline.
            line = proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                return
            stdout_log.append(line)

    import threading

    def _reader() -> None:
        while True:
            line = proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                break
            stdout_log.append(line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    def _wait_for_token(token: str, timeout_s: float) -> bool:
        """Poll ``stdout_log`` for ``token``. Turn 1 pays a ~20 s cold
        CC handshake; subsequent warm turns are ~3 s. We poll instead
        of fixed sleeps so the test isn't racing the LLM proxy."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if any(token in line for line in stdout_log):
                return True
            time.sleep(0.2)
        return False

    try:
        acks = ["ack1", "ack2", "ack3"]
        # First turn has to pay MCP + system_prompt bootstrapping (~20 s
        # cold), warm turns should be <5 s. Give ample buffer so /exit
        # never cuts off turn 3.
        timeouts = [45.0, 20.0, 20.0]
        for i, (msg, tok, t_out) in enumerate(zip(messages, acks, timeouts)):
            print(f"\n[selftest] sending message {i + 1}/{len(messages)}: {msg}")
            proc.stdin.write(msg + "\n")
            proc.stdin.flush()
            got = _wait_for_token(tok, t_out)
            if not got:
                print(
                    f"[selftest][WARN] never saw '{tok}' in stdout within "
                    f"{t_out:.0f} s (will still run profile checks)"
                )

        # Tiny grace so the final stream.result flushes to the profile
        # file before we send /exit.
        time.sleep(1.0)

        print("\n[selftest] sending /exit")
        proc.stdin.write("/exit\n")
        proc.stdin.flush()
    except BrokenPipeError:
        print("[selftest] pipe broke early — host likely crashed; check stdout")
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    print("\n===== HOST STDOUT (tail) =====")
    tail = "".join(stdout_log)
    tail = tail[-4000:] if len(tail) > 4000 else tail
    try:
        sys.stdout.buffer.write(tail.encode("utf-8", "replace"))
        sys.stdout.buffer.write(b"\n")
        sys.stdout.flush()
    except Exception:
        pass

    if not PROFILE_LOG.exists():
        print("[selftest][FAIL] profile log was not produced")
        return 2

    # Keep a copy so the .jsonl isn't overwritten by the next run.
    KEEP_LOG.write_bytes(PROFILE_LOG.read_bytes())

    return _analyse(PROFILE_LOG)


def _analyse(log_path: Path) -> int:
    events: list[dict] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Profile events use ``"evt"`` for the event name; span open/close
    # use ``"evt": "span.open" / "span.close"`` with ``"name"`` carrying
    # the span label. We want instantaneous event() records, so filter
    # by ``evt``.
    def _evt(e: dict, name: str) -> bool:
        return e.get("evt") == name

    opened = [e for e in events if _evt(e, "stream.opened")]
    reused = [e for e in events if _evt(e, "stream.reused")]
    inits = [e for e in events if _evt(e, "stream.session_init")]
    results = [e for e in events if _evt(e, "stream.result")]
    errors = [e for e in events if _evt(e, "stream.sdk_error")]
    # ``meta`` is nested under a dict — flatten lookups for the matrix.
    def _meta(e: dict) -> dict:
        m = e.get("meta")
        return m if isinstance(m, dict) else {}

    print("\n===== Tier 1 event summary =====")
    print(f"  stream.opened       : {len(opened)}")
    print(f"  stream.reused       : {len(reused)}")
    print(f"  stream.session_init : {len(inits)}")
    print(f"  stream.result       : {len(results)}")
    print(f"  stream.sdk_error    : {len(errors)}")

    def _check(label: str, cond: bool, detail: str) -> bool:
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {label}: {detail}")
        return cond

    ok = True

    ok &= _check(
        "B1 exactly 1 stream.opened",
        len(opened) == 1,
        f"got {len(opened)}",
    )
    ok &= _check(
        "B2 >=2 stream.reused (turns 2+3)",
        len(reused) >= 2,
        f"got {len(reused)}",
    )
    ok &= _check(
        "B3 3 stream.result events",
        len(results) == 3,
        f"got {len(results)}",
    )
    if results:
        keys = {_meta(r).get("session_key") for r in results}
        ok &= _check(
            "B4 all turns share one session_key",
            len(keys) == 1,
            f"observed={keys}",
        )
    # Turn 1 session_init is NOT expected to be tiny — the CC CLI's
    # first handshake pays more work than the per-turn recap. What we
    # care about is turns 2+ being the cheap recap (<50 ms). If turn 1
    # is also cheap that's bonus.
    inits_by_turn = {
        int(_meta(e).get("turn") or 0): float(_meta(e).get("since_stream_ms") or 0)
        for e in inits
    }
    for turn_n in (2, 3):
        v = inits_by_turn.get(turn_n)
        ok &= _check(
            f"B5 turn {turn_n} session_init <= 50 ms (recap, not spawn)",
            v is not None and v <= 50.0,
            f"turn{turn_n}.session_init_ms={v}",
        )
    ok &= _check(
        "B6 no stream.sdk_error",
        len(errors) == 0,
        f"errors={[_meta(e).get('err') for e in errors]}",
    )

    # Compare: stream.turn span durations across the three turns.
    turn_spans = [
        e for e in events
        if e.get("name") == "stream.turn"
        and e.get("evt") == "span.close"
        and "dur_ms" in e
    ]
    if len(turn_spans) >= 3:
        t1, t2, t3 = [float(s["dur_ms"]) for s in turn_spans[:3]]
        print(
            f"\n  stream.turn dur_ms   t1={t1:.0f}  t2={t2:.0f}  t3={t3:.0f}  "
            f"(t1-t2={t1 - t2:+.0f} ms, t1-t3={t1 - t3:+.0f} ms)"
        )
        # Soft signal only. Turn 1 includes one-time MCP / hook / system
        # prompt bootstrapping that subsequent turns skip, so a ~10 s+
        # delta is plausible; we don't gate on a specific number.

    print("\nOVERALL: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
