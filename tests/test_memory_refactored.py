"""Tests for MemoryStore read/write and the ``extract_json_array`` utility.

The full reflect + consolidate + dream integration suite is rewritten in
Phase 4.5 (data source migrated to Claude Code JSONL) and Phase 11.
"""

from __future__ import annotations

import json
from pathlib import Path

from pip_agent.memory import MemoryStore
from pip_agent.memory.utils import extract_json_array


class TestExtractJsonArray:
    def test_plain_json(self):
        assert extract_json_array('[{"a": 1}]') == [{"a": 1}]

    def test_markdown_fenced(self):
        text = '```json\n[{"a": 1}]\n```'
        assert extract_json_array(text) == [{"a": 1}]

    def test_empty_array(self):
        assert extract_json_array("[]") == []

    def test_invalid_returns_none(self):
        assert extract_json_array("not json at all") is None


def _pip_boy_store(tmp_path: Path) -> MemoryStore:
    """Build a v2-layout root-agent store rooted at ``tmp_path / .pip``."""
    return MemoryStore(
        agent_dir=tmp_path / ".pip",
        workspace_pip_dir=tmp_path / ".pip",
        agent_id="pip-boy",
    )


class TestMemoryStoreBasics:
    def test_write_observation_appends_to_jsonl(self, tmp_path: Path):
        store = _pip_boy_store(tmp_path)
        store.write_single("first observation", category="observation", source="user")
        observations = list((store.agent_dir / "observations").glob("*.jsonl"))
        assert len(observations) == 1
        lines = [
            json.loads(line)
            for line in observations[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        assert lines[0]["text"] == "first observation"

    def test_load_state_missing_returns_empty_dict(self, tmp_path: Path):
        store = _pip_boy_store(tmp_path)
        assert store.load_state() == {}

    def test_save_and_load_state_roundtrip(self, tmp_path: Path):
        store = _pip_boy_store(tmp_path)
        store.save_state({"last_reflect_at": 12345})
        assert store.load_state() == {"last_reflect_at": 12345}

    def test_write_observations_self_heals_missing_parent(
        self, tmp_path: Path,
    ):
        """Regression: a long-lived host caches ``MemoryStore`` across
        ``/subagent reset``, which wipes ``.pip/`` out from under it. The
        next reflect must not die with ENOENT on
        ``observations/<date>.jsonl`` — ``write_observations`` re-creates
        the parent dir on demand the same way ``atomic_write`` does for
        ``state.json``.
        """
        import shutil

        store = _pip_boy_store(tmp_path)
        shutil.rmtree(store.agent_dir / "observations")
        assert not (store.agent_dir / "observations").exists()

        store.write_single("after-reset obs", category="observation")

        obs_files = list((store.agent_dir / "observations").glob("*.jsonl"))
        assert len(obs_files) == 1
        lines = [
            json.loads(line)
            for line in obs_files[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert lines and lines[0]["text"] == "after-reset obs"


class TestPurgeObservations:
    """H5 regression guard: Dream must be able to hard-delete consumed
    observations so consolidate doesn't re-weight the same batch every
    night. Purge is cutoff-based so an observation written while Dream
    is running (ts > cutoff) survives.
    """

    def _seed(self, store: MemoryStore, rows: list[dict]) -> None:
        for obs in rows:
            store.write_observations([obs])

    def test_deletes_lines_at_or_before_cutoff(self, tmp_path: Path):
        store = _pip_boy_store(tmp_path)
        self._seed(store, [
            {"ts": 1.0, "text": "old 1", "category": "decision", "source": "auto"},
            {"ts": 2.0, "text": "old 2", "category": "decision", "source": "auto"},
            {"ts": 3.0, "text": "new", "category": "decision", "source": "auto"},
        ])

        purged = store.purge_observations_through(2.0)
        assert purged == 2
        remaining = store.load_all_observations()
        assert [o["text"] for o in remaining] == ["new"]

    def test_empty_file_is_unlinked(self, tmp_path: Path):
        store = _pip_boy_store(tmp_path)
        self._seed(store, [
            {"ts": 1.0, "text": "dropped", "category": "decision", "source": "auto"},
        ])
        obs_dir = store.agent_dir / "observations"
        files_before = list(obs_dir.glob("*.jsonl"))
        assert files_before

        store.purge_observations_through(10.0)
        assert not list(obs_dir.glob("*.jsonl"))

    def test_keeps_unparseable_lines(self, tmp_path: Path):
        """Malformed JSON lines are held back from purge — destroying
        bytes we can't even decode is worse than keeping noise."""
        store = _pip_boy_store(tmp_path)
        obs_dir = store.agent_dir / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        fp = obs_dir / "2025-01-01.jsonl"
        fp.write_text(
            '{"ts": 1.0, "text": "old", "category": "x", "source": "auto"}\n'
            "not json at all\n"
            '{"ts": 3.0, "text": "new", "category": "x", "source": "auto"}\n',
            encoding="utf-8",
        )

        purged = store.purge_observations_through(2.0)
        assert purged == 1
        remaining_raw = fp.read_text(encoding="utf-8")
        assert "not json at all" in remaining_raw
        assert "old" not in remaining_raw

    def test_cutoff_before_all_obs_keeps_everything(self, tmp_path: Path):
        """Mid-Dream race: if reflect wrote observations between the
        Dream started_at capture and the purge, their ts > cutoff and
        they survive intact.
        """
        store = _pip_boy_store(tmp_path)
        self._seed(store, [
            {"ts": 10.0, "text": "a", "category": "x", "source": "auto"},
            {"ts": 11.0, "text": "b", "category": "x", "source": "auto"},
        ])
        purged = store.purge_observations_through(5.0)
        assert purged == 0
        assert len(store.load_all_observations()) == 2


# ---------------------------------------------------------------------------
# enrich_prompt: owner.md injection + heading tolerance
# ---------------------------------------------------------------------------


class TestEnrichPromptOwnerInjection:
    """Regression guard for a two-part bug:

    1. ``_IDENTITY_RE`` only matched ``## Identity`` (two hashes), so
       the shipped scaffold (``# Identity``, single hash) fell out
       of the fast-path and the owner block was prepended *before*
       the Identity heading — a semantically wrong location that
       some models latched onto as "chrome" instead of context.
    2. Sub-agents shared the workspace-level ``owner.md`` but their
       persona bodies were a 4-line stub with no guidance about
       how to read it, so they happily answered "I don't know who
       you are" while the owner data sat right there in the prompt.

    We cover the injection-position half here; the persona-inheritance
    half is exercised in ``tests/test_host_commands.py::TestAgentCommand``.
    """

    def _store_with_owner(
        self, tmp_path: Path, owner_body: str,
    ) -> MemoryStore:
        workspace_pip = tmp_path / ".pip"
        workspace_pip.mkdir(parents=True)
        (workspace_pip / "owner.md").write_text(owner_body, encoding="utf-8")
        agent_dir = tmp_path / "sub" / ".pip"
        agent_dir.mkdir(parents=True)
        return MemoryStore(
            agent_dir=agent_dir,
            workspace_pip_dir=workspace_pip,
            agent_id="sub",
        )

    def test_single_hash_identity_injects_user_section_after_identity(
        self, tmp_path: Path,
    ):
        """Scaffold-style ``# Identity`` headings must not bypass the
        post-Identity injection point."""
        store = self._store_with_owner(
            tmp_path, "- `cli:cli-user` — Owner (Eric)",
        )
        base = (
            "# Identity\n\nYou are Sub.\n\n"
            "# Core Philosophy\n\nKeep it simple.\n"
        )
        out = store.enrich_prompt(
            base, user_text="hello", channel="cli", agent_id="sub",
            workdir=str(tmp_path / "sub"), sender_id="cli-user",
        )
        assert "Eric" in out
        identity_pos = out.find("# Identity")
        user_pos = out.find("## User")
        philosophy_pos = out.find("# Core Philosophy")
        assert 0 <= identity_pos < user_pos < philosophy_pos, out

    def test_double_hash_identity_still_works(self, tmp_path: Path):
        """Legacy ``## Identity`` (two hashes) must still match — the
        builtin fallback persona uses that form."""
        store = self._store_with_owner(
            tmp_path, "- `cli:cli-user` — Owner (Eric)",
        )
        base = "## Identity\n\nYou are Sub.\n\n## Rules\n\nBe kind.\n"
        out = store.enrich_prompt(
            base, user_text="hello", channel="cli", agent_id="sub",
            workdir=str(tmp_path / "sub"), sender_id="cli-user",
        )
        identity_pos = out.find("## Identity")
        user_pos = out.find("## User")
        rules_pos = out.find("## Rules")
        assert 0 <= identity_pos < user_pos < rules_pos, out

    def test_sub_agent_sees_workspace_owner(self, tmp_path: Path):
        """The whole point of the bug report: the sub-agent's
        ``MemoryStore`` has ``agent_dir = <workspace>/sub/.pip`` but
        ``workspace_pip_dir = <workspace>/.pip``, and ``owner.md``
        lives only at the latter. The injected content must come
        from the workspace-level file."""
        store = self._store_with_owner(
            tmp_path,
            "# Owner\n- `cli:cli-user` — Eric (Pacific time)\n",
        )
        out = store.enrich_prompt(
            "# Identity\n\nYou are Sub.\n",
            user_text="",
            channel="cli",
            agent_id="sub",
            workdir=str(tmp_path / "sub"),
            sender_id="cli-user",
        )
        assert "Eric" in out
        assert "Pacific time" in out

    def test_no_owner_file_does_not_inject_user_section(
        self, tmp_path: Path,
    ):
        """No owner file + no per-agent users → no ``## User``
        section at all, not an empty one."""
        workspace_pip = tmp_path / ".pip"
        workspace_pip.mkdir(parents=True)
        agent_dir = tmp_path / "sub" / ".pip"
        agent_dir.mkdir(parents=True)
        store = MemoryStore(
            agent_dir=agent_dir,
            workspace_pip_dir=workspace_pip,
            agent_id="sub",
        )
        out = store.enrich_prompt(
            "# Identity\n\nYou are Sub.\n",
            user_text="",
            channel="cli",
            agent_id="sub",
            workdir=str(tmp_path / "sub"),
            sender_id="cli-user",
        )
        assert "## User" not in out
