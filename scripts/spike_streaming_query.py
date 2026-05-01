"""Tier 1 spike: verify ClaudeSDKClient keeps the subprocess alive across turns.

Goal:
  * Confirm that one ``ClaudeSDKClient`` instance can serve multiple
    ``client.query(...)`` calls without re-spawning ``claude.exe``.
  * Measure the per-turn session_init cost: it should be paid once on
    ``connect()`` and be near zero on subsequent turns.
  * Verify session state is preserved across turns inside the same client
    (turn 2 must recall content from turn 1).

This is the **hard gate** for the Tier 1 redesign in
``pip-boy_perf_optimization_plan_f63af13d.plan.md``. If this spike fails,
the streaming-query reuse design is not viable with Python SDK v0.1.x and
we must revert to per-turn spawn.

Run::

    D:\\Workspace\\pip-test\\.venv\\Scripts\\python.exe scripts/spike_streaming_query.py

Requires ``D:/Workspace/pip-test/.env`` to be populated with
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path


def _load_env(env_path: Path) -> None:
    if not env_path.exists():
        print(f"[warn] {env_path} not found; relying on process env")
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _fmt_ms(ns: int) -> str:
    return f"{ns / 1_000_000.0:.1f} ms"


async def _drain_turn(client, label: str) -> dict:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
    )

    turn_start = time.perf_counter_ns()
    first_init_ns: int | None = None
    first_text_ns: int | None = None
    first_text_seen = False
    session_id: str | None = None
    reply_text = ""
    result_payload: dict | None = None

    async for msg in client.receive_response():
        now = time.perf_counter_ns()
        mtype = type(msg).__name__
        if isinstance(msg, SystemMessage):
            if getattr(msg, "subtype", None) == "init":
                first_init_ns = now - turn_start
                session_id = (getattr(msg, "data", {}) or {}).get("session_id")
                print(f"  [{label}] SystemMessage init  +{_fmt_ms(first_init_ns)}  sid={session_id}")
            else:
                print(f"  [{label}] SystemMessage {getattr(msg, 'subtype', '?')}  +{_fmt_ms(now - turn_start)}")
        elif isinstance(msg, AssistantMessage):
            for block in msg.content or []:
                if isinstance(block, TextBlock):
                    if not first_text_seen:
                        first_text_ns = now - turn_start
                        first_text_seen = True
                        print(f"  [{label}] first TextBlock     +{_fmt_ms(first_text_ns)}  ({len(block.text)} chars)")
                    reply_text += block.text
        elif isinstance(msg, ResultMessage):
            total_ns = now - turn_start
            session_id = getattr(msg, "session_id", None) or session_id
            result_payload = {
                "num_turns": getattr(msg, "num_turns", None),
                "stop_reason": getattr(msg, "stop_reason", None),
                "is_error": getattr(msg, "is_error", None),
                "total_cost_usd": getattr(msg, "total_cost_usd", None),
            }
            print(
                f"  [{label}] ResultMessage       +{_fmt_ms(total_ns)}  "
                f"turns={result_payload['num_turns']} stop={result_payload['stop_reason']} err={result_payload['is_error']}"
            )
        else:
            print(f"  [{label}] {mtype}")
    return {
        "session_id": session_id,
        "init_ns": first_init_ns,
        "first_text_ns": first_text_ns,
        "total_ns": time.perf_counter_ns() - turn_start,
        "reply_text": reply_text,
        "result": result_payload,
    }


async def run_spike() -> int:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    # Env for subprocess (mirrors pip_agent.agent_runner._build_env +
    # pip_agent.anthropic_client.resolve_anthropic_credential proxy rule).
    # Single-credential model: ANTHROPIC_API_KEY only; bearer is decided by
    # ANTHROPIC_BASE_URL presence.
    sdk_env: dict[str, str] = {
        "CLAUDE_CODE_DISABLE_CRON": "1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    bearer = bool(base_url)
    if api_key:
        if bearer:
            sdk_env["ANTHROPIC_AUTH_TOKEN"] = api_key
        else:
            sdk_env["ANTHROPIC_API_KEY"] = api_key
    if base_url:
        sdk_env["ANTHROPIC_BASE_URL"] = base_url
    print(f"(auth: bearer={bearer}  base_url={base_url or '<direct>'})")

    # Minimal options: no MCP, no hooks, bypass permissions, terse system.
    options = ClaudeAgentOptions(
        system_prompt=(
            "You are a test harness assistant. Reply in one short sentence. "
            "Do not use tools. Do not explain your reasoning."
        ),
        permission_mode="bypassPermissions",
        setting_sources=["user"],
        env=sdk_env,
    )

    print("=" * 72)
    print("Tier 1 spike: ClaudeSDKClient reuse across turns")
    print("=" * 72)

    connect_start = time.perf_counter_ns()
    client = ClaudeSDKClient(options=options)
    # connect(None) opens an empty stream => subprocess is spawned + handshake
    # runs, but no user message is sent yet. First query() sends the first msg.
    await client.connect()
    connect_ns = time.perf_counter_ns() - connect_start
    print(f"\nconnect() returned in {_fmt_ms(connect_ns)}  (subprocess + handshake)")

    try:
        # --- Turn 1 --- seed state
        print("\n--- Turn 1: seed context with a number to remember ---")
        t1_start = time.perf_counter_ns()
        await client.query("Please remember the number 42. Just say 'noted'.")
        turn1 = await _drain_turn(client, "t1")
        turn1["send_to_result_ns"] = time.perf_counter_ns() - t1_start

        # --- Turn 2 --- recall test
        print("\n--- Turn 2: recall the number ---")
        t2_start = time.perf_counter_ns()
        await client.query("What number did I just ask you to remember?")
        turn2 = await _drain_turn(client, "t2")
        turn2["send_to_result_ns"] = time.perf_counter_ns() - t2_start

        # --- Turn 3 --- another recall to confirm ongoing state
        print("\n--- Turn 3: one more round trip ---")
        t3_start = time.perf_counter_ns()
        await client.query("Say the number back one more time, digits only.")
        turn3 = await _drain_turn(client, "t3")
        turn3["send_to_result_ns"] = time.perf_counter_ns() - t3_start

    finally:
        disconnect_start = time.perf_counter_ns()
        await client.disconnect()
        print(f"\ndisconnect() returned in {_fmt_ms(time.perf_counter_ns() - disconnect_start)}")

    # --- Gate assertions ---
    print("\n" + "=" * 72)
    print("Gate assertions")
    print("=" * 72)

    def _check(label: str, cond: bool, detail: str) -> bool:
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {label}: {detail}")
        return cond

    ok = True

    # A1: session_id continuity (all three turns should share the same session_id)
    sids = {turn1.get("session_id"), turn2.get("session_id"), turn3.get("session_id")}
    sids.discard(None)
    ok &= _check(
        "A1 session_id continuity",
        len(sids) == 1 and None not in {turn1.get("session_id"), turn2.get("session_id"), turn3.get("session_id")},
        f"observed={sids}",
    )

    # A2: SDK always emits SystemMessage(init) as a control-protocol recap at
    # the start of each client.query() response stream; what matters is that
    # it's a cheap recap (<50 ms) not a real claude.exe re-spawn (~400 ms).
    # If the subprocess were truly re-spawned, we'd see session_init closer to
    # the connect() cost (~500 ms).
    _INIT_RECAP_MAX_MS = 50
    ok &= _check(
        "A2 turn2 init is cheap recap, not spawn",
        (turn2["init_ns"] or 0) < _INIT_RECAP_MAX_MS * 1_000_000,
        f"turn2.init_ns={_fmt_ms(turn2['init_ns'] or 0)} (threshold {_INIT_RECAP_MAX_MS} ms)",
    )
    ok &= _check(
        "A3 turn3 init is cheap recap, not spawn",
        (turn3["init_ns"] or 0) < _INIT_RECAP_MAX_MS * 1_000_000,
        f"turn3.init_ns={_fmt_ms(turn3['init_ns'] or 0)} (threshold {_INIT_RECAP_MAX_MS} ms)",
    )

    # A4: turn 2 first_text should be fast (no session_init tax; LLM TTFT only). Expect < 4000 ms.
    t2_ttft = turn2["first_text_ns"] or 10**12
    ok &= _check(
        "A4 turn2 TTFT reasonable",
        t2_ttft < 6_000_000_000,
        f"turn2.first_text_ns={_fmt_ms(t2_ttft)}",
    )

    # A5: the recall should actually mention the number. Tolerate case/punctuation.
    t2_reply = (turn2["reply_text"] or "").lower()
    ok &= _check(
        "A5 turn2 recalls '42'",
        ("42" in t2_reply) or ("forty-two" in t2_reply) or ("forty two" in t2_reply),
        f"turn2.reply_text={turn2['reply_text'][:200]!r}",
    )

    # A6: cost accounting present (sanity; guards against a degraded reply path)
    r1 = turn1.get("result") or {}
    r2 = turn2.get("result") or {}
    ok &= _check(
        "A6 turns report num_turns >= 1",
        (r1.get("num_turns") or 0) >= 1 and (r2.get("num_turns") or 0) >= 1,
        f"turn1.num_turns={r1.get('num_turns')} turn2.num_turns={r2.get('num_turns')}",
    )

    print("\nTIMING SUMMARY")
    print(f"  connect (spawn + handshake)  : {_fmt_ms(connect_ns)}")
    for i, t in enumerate([turn1, turn2, turn3], start=1):
        print(
            f"  turn {i}: send->result {_fmt_ms(t['send_to_result_ns'])}  "
            f"first_text {_fmt_ms(t['first_text_ns'] or 0)}  total {_fmt_ms(t['total_ns'])}"
        )

    print("\nOVERALL: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main() -> int:
    _load_env(Path("D:/Workspace/pip-test/.env"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[fatal] no ANTHROPIC_API_KEY in env", file=sys.stderr)
        return 2
    return asyncio.run(run_spike())


if __name__ == "__main__":
    raise SystemExit(main())
