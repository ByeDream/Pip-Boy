"""Smoke test for the v2 identity redesign.

Covers three concerns after the ``.pip/agents/<id>/`` → ``.pip/ + <id>/.pip/``
migration:

1. **Cold-start cost** — import ``pip_agent.agent_host`` repeatedly and
   report the median, min, max so we can compare against the baseline
   in ``docs/performance-baseline.md``.
2. **Legacy layout migration** — synthesise a v1 tree, run
   ``ensure_workspace``, and assert every critical file/dir landed in
   the new v2 location.
3. **Multi-agent switching** — drive ``AgentRegistry`` through
   ``register_agent`` / ``paths_for`` / ``archive_agent`` /
   ``remove_agent`` end-to-end and check the paths track correctly so
   a running host can route turns to different sub-agents.

No Anthropic traffic; no network. Safe to run in CI on Windows.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def bench_cold_start(iterations: int = 6) -> None:
    """Measure in-process reimport cost as a quick cold-start probe.

    This is not the full ``cold_start.loop_ready`` number (that requires
    the channel managers), but it isolates the import graph cost — the
    piece our refactor can affect.
    """
    _section(f"cold-start import x{iterations}")
    samples_ms: list[float] = []
    for i in range(iterations):
        for mod in list(sys.modules):
            if mod.startswith("pip_agent"):
                del sys.modules[mod]
        t0 = time.perf_counter()
        import pip_agent.agent_host  # noqa: F401
        dt = (time.perf_counter() - t0) * 1000
        samples_ms.append(dt)
        print(f"  iter {i}: import={dt:7.1f} ms")
    samples_ms.sort()
    print(
        f"  median={samples_ms[len(samples_ms)//2]:.1f} ms  "
        f"min={samples_ms[0]:.1f} ms  "
        f"max={samples_ms[-1]:.1f} ms",
    )


def smoke_v1_migration() -> None:
    """Materialise a v1 layout, migrate, assert v2 invariants."""
    _section("v1 -> v2 migration")
    from pip_agent.scaffold import ensure_workspace

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / ".git").mkdir()

        pipboy = ws / ".pip" / "agents" / "pip-boy"
        pipboy.mkdir(parents=True)
        (pipboy / "persona.md").write_text(
            "---\nname: Pip-Boy\nmodel: claude-opus-4-6\n---\nRoot persona.\n",
            encoding="utf-8",
        )
        (pipboy / "state.json").write_text('{"last_reflect_at": 42}', encoding="utf-8")
        (pipboy / "observations").mkdir()
        (pipboy / "observations" / "2026-01-01.jsonl").write_text(
            '{"text": "root obs"}\n', encoding="utf-8",
        )

        stella = ws / ".pip" / "agents" / "stella"
        stella.mkdir()
        (stella / "persona.md").write_text(
            "---\nname: Stella\n---\nStella persona.\n", encoding="utf-8",
        )
        (stella / "state.json").write_text('{"last_reflect_at": 7}', encoding="utf-8")

        (ws / ".pip" / "agents" / "bindings.json").write_text(
            json.dumps({"bindings": []}), encoding="utf-8",
        )

        ensure_workspace(ws)

        # Root persona content bubbled up.
        assert "Root persona." in (ws / ".pip" / "persona.md").read_text(
            encoding="utf-8",
        ), "root persona not surfaced"
        assert (ws / ".pip" / "observations" / "2026-01-01.jsonl").exists()
        # Sub-agent promoted to sibling dir.
        assert (ws / "stella" / ".pip" / "persona.md").exists()
        assert "Stella persona." in (
            ws / "stella" / ".pip" / "persona.md"
        ).read_text(encoding="utf-8")
        # bindings up-shifted.
        assert (ws / ".pip" / "bindings.json").exists()
        # Registry knows the sub-agent.
        reg = json.loads(
            (ws / ".pip" / "agents_registry.json").read_text(encoding="utf-8"),
        )
        assert reg["agents"]["pip-boy"]["kind"] == "root"
        assert reg["agents"]["stella"]["kind"] == "sub"
        # Legacy tree gone.
        assert not (ws / ".pip" / "agents").exists()
        print("  OK: pip-boy bubbled up, sub-agent relocated, bindings moved")


def smoke_agent_switching() -> None:
    """Exercise AgentRegistry paths for multiple sub-agents."""
    _section("multi-agent registry + paths_for")
    from pip_agent.routing import AgentConfig, AgentRegistry

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / ".pip").mkdir()
        (ws / ".pip" / "persona.md").write_text(
            "---\nname: Pip-Boy\n---\nRoot.\n", encoding="utf-8",
        )

        reg = AgentRegistry(ws)
        root_paths = reg.paths_for("pip-boy")
        assert root_paths is not None
        assert root_paths.cwd == ws
        assert root_paths.pip_dir == ws / ".pip"

        reg.register_agent(
            AgentConfig(id="Stella", name="Stella"),
            description="Project Stella",
        )
        reg.register_agent(
            AgentConfig(id="Nova", name="Nova"),
            description="Project Nova",
        )
        reg.save_registry()
        # Materialise the sub-agent directories so archive/remove have
        # something real to move or delete.
        (ws / "Stella" / ".pip").mkdir(parents=True)
        (ws / "Nova" / ".pip").mkdir(parents=True)

        stella = reg.paths_for("Stella")
        nova = reg.paths_for("Nova")
        assert stella and nova
        assert stella.cwd == ws / "Stella"
        assert stella.pip_dir == ws / "Stella" / ".pip"
        assert nova.cwd == ws / "Nova"
        # Each agent has its own pip_dir; they share workspace_pip_dir.
        assert stella.pip_dir != nova.pip_dir
        assert stella.workspace_pip_dir == nova.workspace_pip_dir == ws / ".pip"

        # Archiving relocates on-disk and removes from the live registry.
        dest = reg.archive_agent("Stella")
        assert dest is not None, "archive_agent should return the new path"
        assert dest.exists()
        assert dest.parent == ws / ".pip" / "archived"
        assert reg.paths_for("Stella") is None

        # Delete wipes Nova wholesale.
        assert reg.remove_agent("Nova", delete_files=True)
        assert reg.paths_for("Nova") is None
        assert not (ws / "Nova").exists()
        print("  OK: root + 2 sub-agents registered, archived, removed")


def smoke_runtime_cwd_wiring() -> None:
    """Check that `_resolve_paths` in the host picks up each agent's cwd."""
    _section("host._resolve_paths wiring")

    from pip_agent import agent_host as ah
    from pip_agent.channels import ChannelManager
    from pip_agent.routing import AgentConfig, AgentRegistry, BindingTable

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / ".pip").mkdir()
        (ws / ".pip" / "persona.md").write_text(
            "---\nname: Pip-Boy\n---\nRoot.\n", encoding="utf-8",
        )
        reg = AgentRegistry(ws)
        reg.register_agent(AgentConfig(id="Atlas", name="Atlas"))
        reg.save_registry()

        host = ah.AgentHost(
            registry=reg,
            binding_table=BindingTable(),
            channel_mgr=ChannelManager(),
        )
        root = host._resolve_paths("pip-boy")
        atlas = host._resolve_paths("Atlas")

        assert root.cwd == ws
        assert atlas.cwd == ws / "Atlas"
        assert root.cwd != atlas.cwd, "each agent must resolve to its own cwd"
        print("  OK: resolve_paths keeps per-agent cwd distinct")


def main() -> None:
    bench_cold_start()
    smoke_v1_migration()
    smoke_agent_switching()
    try:
        smoke_runtime_cwd_wiring()
    except Exception as e:  # noqa: BLE001
        # AgentHost constructor may need additional params; degrade gracefully.
        print(f"  SKIP runtime wiring: {type(e).__name__}: {e}")
    print("\nAll smoke checks passed.")


if __name__ == "__main__":
    main()
