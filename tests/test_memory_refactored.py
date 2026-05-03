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
# Addressbook: lazy-load model (enrich_prompt never injects contacts)
# ---------------------------------------------------------------------------


class TestAddressbookLazyLoad:
    """The addressbook moved from eager-inject-into-system-prompt to
    lazy ``lookup_user``-on-demand. These tests encode the two new
    invariants this refactor is supposed to guarantee:

    1. ``enrich_prompt`` must NEVER materialise contact content into
       the system prompt, no matter how many profiles exist. Prompt
       cost stays flat as the addressbook grows.
    2. ``find_profile_by_sender`` still works across the root/sub-agent
       split (shared workspace addressbook), and the returned
       ``user_id`` is stable — it's what the model sees on every
       ``<user_query user_id=...>`` wrapper.
    """

    def _seed_sub_agent_store(
        self, tmp_path: Path,
    ) -> tuple[MemoryStore, Path]:
        workspace_pip = tmp_path / ".pip"
        workspace_pip.mkdir(parents=True)
        agent_dir = tmp_path / "sub" / ".pip"
        agent_dir.mkdir(parents=True)
        store = MemoryStore(
            agent_dir=agent_dir,
            workspace_pip_dir=workspace_pip,
            agent_id="sub",
        )
        return store, workspace_pip

    def test_enrich_prompt_never_injects_addressbook(self, tmp_path: Path):
        store, _ = self._seed_sub_agent_store(tmp_path)
        # Populate the workspace addressbook with a contact whose
        # fields are obvious nonsense tokens — they MUST be unique
        # enough that ``not in out`` can't be tripped by the rendered
        # ``workdir`` (``tmp_path`` typically embeds the OS username,
        # so realistic names like "Eric" alias straight onto a
        # ``C:\Users\Eric...`` substring on dev machines).
        store.upsert_contact(
            sender_id="cli-user", channel="cli",
            name="Zorblax", call_me="Zorblax",
            notes="addressbook-leak-canary-xyzzy",
        )

        out = store.enrich_prompt(
            "# Identity\n\nYou are Sub.\n",
            user_text="hello",
            channel="cli", agent_id="sub",
            workdir=str(tmp_path / "sub"), sender_id="cli-user",
        )
        assert "## Addressbook" not in out
        assert "## User" not in out
        assert "Zorblax" not in out
        assert "addressbook-leak-canary-xyzzy" not in out

    def test_empty_addressbook_is_also_silent(self, tmp_path: Path):
        """No contacts → no placeholder heading either."""
        store, _ = self._seed_sub_agent_store(tmp_path)
        out = store.enrich_prompt(
            "# Identity\n\nYou are Sub.\n",
            user_text="", channel="cli", agent_id="sub",
            workdir=str(tmp_path / "sub"), sender_id="cli-user",
        )
        assert "## Addressbook" not in out

    def test_sub_agent_upsert_contact_lands_in_root_addressbook(
        self, tmp_path: Path,
    ):
        """Shared addressbook invariant: the sub-agent's
        ``upsert_contact`` must land in the workspace root's
        ``addressbook/``, not a local sub-agent copy."""
        store, workspace_pip = self._seed_sub_agent_store(tmp_path)
        uid, msg = store.upsert_contact(
            sender_id="alice", channel="wecom",
            name="Alice", call_me="Alice",
        )
        assert uid and len(uid) == 16
        root_ab = workspace_pip / "addressbook"
        assert (root_ab / f"{uid}.md").is_file()
        # Sub-agent dir stays addressbook-free.
        agent_dir = store.agent_dir
        assert not (agent_dir / "addressbook").exists()
        assert not (agent_dir / "users").exists()

    def test_find_profile_by_sender_returns_uuid_stem(self, tmp_path: Path):
        """The user_id written to ``<user_query>`` is the filename stem
        returned by ``find_profile_by_sender`` — this test pins that
        contract so a refactor of the storage layout can't silently
        break the prompt wrapper."""
        store, _ = self._seed_sub_agent_store(tmp_path)
        uid, _ = store.upsert_contact(
            sender_id="alice", channel="wecom", name="Alice",
        )
        path = store.find_profile_by_sender("wecom", "alice")
        assert path is not None
        assert store.extract_user_id(path) == uid

    def test_load_profile_by_id_roundtrips_written_fields(
        self, tmp_path: Path,
    ):
        """``lookup_user`` (which calls ``load_profile_by_id``) must
        see every field ``remember_user`` wrote. This is the lazy-load
        round-trip the new design depends on."""
        store, _ = self._seed_sub_agent_store(tmp_path)
        uid, _ = store.upsert_contact(
            sender_id="alice", channel="cli",
            name="Alice", call_me="Ali", timezone="Asia/Shanghai",
            notes="prefers terse replies",
        )
        body = store.load_profile_by_id(uid)
        assert body is not None
        assert "Alice" in body
        assert "Ali" in body
        assert "Asia/Shanghai" in body
        assert "prefers terse replies" in body
        assert "`cli:alice`" in body

    def test_load_profile_by_id_rejects_bad_ids(self, tmp_path: Path):
        """Non-hex / wrong-length inputs must not resolve to anything —
        we don't want ``lookup_user("../../passwd")`` to escape the
        addressbook dir via filename shenanigans."""
        store, _ = self._seed_sub_agent_store(tmp_path)
        assert store.load_profile_by_id("") is None
        assert store.load_profile_by_id("not-hex!") is None
        assert store.load_profile_by_id("../passwd") is None
        assert store.load_profile_by_id("deadbeef") is None  # well-formed but absent

    def test_update_contact_appends_new_identifier(self, tmp_path: Path):
        """A verified user reaching out from a fresh channel should be
        recognisable next time — ``update_contact`` appends the new
        ``channel:sender_id`` to the Identifiers list."""
        store, _ = self._seed_sub_agent_store(tmp_path)
        uid, _ = store.upsert_contact(
            sender_id="alice", channel="cli", name="Alice",
        )
        store.update_contact(
            uid, sender_id="alice-wecom", channel="wecom",
            notes="reached via WeCom",
        )
        body = store.load_profile_by_id(uid)
        assert body is not None
        assert "`cli:alice`" in body
        assert "`wecom:alice-wecom`" in body
        assert "reached via WeCom" in body

    def test_update_contact_rejects_unknown_user_id(self, tmp_path: Path):
        """Updating a non-existent id is a clear error — we must not
        silently mint a blank profile at that id."""
        store, _ = self._seed_sub_agent_store(tmp_path)
        result = store.update_contact("deadbeef", name="X")
        assert "No contact" in result
        assert not list(store.addressbook_dir.glob("*.md"))
