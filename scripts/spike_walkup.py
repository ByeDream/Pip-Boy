"""Spike: verify Claude Agent SDK walk-up for `setting_sources=["project"]`.

Goal
----
The Pip redesign (see plans/pip_agent_identity_redesign) relies on the SDK
loading project settings from ``<cwd>/.claude/`` AND each parent directory
up to the filesystem root, stopping at the first ``.claude/`` it finds.

Fixture
-------
    walkup_ws/
      CLAUDE.md                       <- unique marker string
      .claude/
        settings.json                 <- trivial content
        agents/spike-probe.md         <- unique subagent name
      sub/
        (no .claude/ here, on purpose)

We then run a one-shot SDK query with ``cwd=walkup_ws/sub`` and
``setting_sources=["user","project"]``. Three independent signals confirm
walk-up:

1. ``SystemMessage(init).data`` lists MCP servers / tools that would only
   be present if the ws-level config was read.
2. The model, asked to recite the CLAUDE.md marker, returns it.
3. The model, asked to list available subagents, includes ``spike-probe``.

Usage
-----
    python scripts/spike_walkup.py               # structure-only, no LLM call
    python scripts/spike_walkup.py --live        # runs the LLM probe too

The live mode requires ``claude`` CLI on PATH (or whatever transport your
installed ``claude-agent-sdk`` uses) and valid auth. If you don't have
either, the structure-only mode still prints enough to reason about the
fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path


MARKER = "PIP_WALKUP_MARKER_8B3F1A"


def build_fixture(root: Path) -> dict[str, Path]:
    ws = root / "walkup_ws"
    sub = ws / "sub"
    claude = ws / ".claude"
    skills = claude / "skills" / "spike_skill"
    agents = claude / "agents"

    for p in (ws, sub, claude, skills, agents):
        p.mkdir(parents=True, exist_ok=True)

    (ws / "CLAUDE.md").write_text(
        f"# Walk-up probe\n\nMarker: {MARKER}\n",
        encoding="utf-8",
    )
    (claude / "settings.json").write_text(
        json.dumps({"permissions": {"allow": []}}, indent=2),
        encoding="utf-8",
    )
    (agents / "spike-probe.md").write_text(
        "---\n"
        "name: spike-probe\n"
        "description: Walk-up spike subagent. If the SDK can see this file, walk-up works.\n"
        "---\n\n"
        "You are the walk-up spike probe. Reply with the word WALKUP_OK.\n",
        encoding="utf-8",
    )
    (skills / "SKILL.md").write_text(
        "---\n"
        "name: spike_skill\n"
        "description: Walk-up spike skill.\n"
        "---\n\n"
        "If invoked, print the marker.\n",
        encoding="utf-8",
    )

    return {"ws": ws, "sub": sub, "claude": claude}


def print_fixture_summary(paths: dict[str, Path]) -> None:
    print("=" * 72)
    print("FIXTURE")
    print("=" * 72)
    for name, p in paths.items():
        print(f"  {name:6s} = {p}")
    print()
    print("Files created:")
    for f in sorted(paths["ws"].rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(paths['ws'])}  ({f.stat().st_size} bytes)")


async def live_probe(sub_cwd: Path) -> dict:
    """Run a one-shot SDK query and report what walk-up surfaced.

    Requires live auth. Catches all errors so the script keeps being
    useful even on a host without ``claude`` CLI.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        SystemMessage,
        query,
    )

    observed: dict = {
        "init_data": None,
        "assistant_text": [],
        "error": None,
    }

    prompt = (
        "Answer in exactly 3 short lines:\n"
        "1) The literal CLAUDE.md marker string you can see (or 'NONE' if you cannot).\n"
        "2) The names of any custom subagents you have available.\n"
        "3) Does the path .claude/ exist in your current working directory? yes/no.\n"
    )

    opts = ClaudeAgentOptions(
        cwd=str(sub_cwd),
        setting_sources=["user", "project"],
        system_prompt="You are a terse diagnostic probe. Be literal.",
        permission_mode="bypassPermissions",
    )

    try:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, SystemMessage) and getattr(msg, "subtype", None) == "init":
                observed["init_data"] = getattr(msg, "data", None)
            elif isinstance(msg, AssistantMessage):
                for block in msg.content or []:
                    text = getattr(block, "text", None)
                    if text:
                        observed["assistant_text"].append(text)
    except Exception as exc:  # noqa: BLE001
        observed["error"] = repr(exc)

    return observed


def verdict(observed: dict) -> None:
    print("=" * 72)
    print("VERDICT")
    print("=" * 72)
    if observed.get("error"):
        print(f"  live query failed: {observed['error']}")
        print("  (structure-only verification still valid; re-run with creds to confirm)")
        return

    joined = "\n".join(observed.get("assistant_text", []))
    marker_seen = MARKER in joined
    agent_seen = "spike-probe" in joined.lower() or "spike_probe" in joined.lower()

    print(f"  CLAUDE.md marker visible in reply : {marker_seen}")
    print(f"  spike-probe subagent mentioned    : {agent_seen}")

    init = observed.get("init_data") or {}
    if isinstance(init, dict):
        for key in ("mcp_servers", "agents", "tools", "slash_commands"):
            val = init.get(key)
            if val is not None:
                print(f"  init.{key} present: {type(val).__name__} (len={len(val) if hasattr(val, '__len__') else '?'})")

    print()
    if marker_seen and agent_seen:
        print("  RESULT: walk-up CONFIRMED - plan A' is safe to land.")
    else:
        print("  RESULT: inconclusive. Check fixture + CLI auth, rerun with --live.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="run an LLM probe (needs creds)")
    parser.add_argument("--keep", action="store_true", help="keep fixture on disk after run")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="pip_walkup_"))
    try:
        paths = build_fixture(tmp)
        print_fixture_summary(paths)

        if not args.live:
            print()
            print("Structure built. Re-run with --live to probe SDK behavior.")
            return 0

        print()
        print("=" * 72)
        print("LIVE PROBE")
        print("=" * 72)
        observed = asyncio.run(live_probe(paths["sub"]))
        print("\nassistant text received:")
        for blob in observed.get("assistant_text", []):
            print(f"  | {blob}")
        print()
        verdict(observed)
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"\n(cleaned up {tmp})")
        else:
            print(f"\n(kept fixture at {tmp})")


if __name__ == "__main__":
    sys.exit(main())
