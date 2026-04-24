"""Tests for the boot-time sweep that drops pre-multi-account artefacts.

The user chose "fresh start" for migration: the one-time sweep in
``agent_host._sweep_legacy_wechat`` deletes
``<state_dir>/wechat_session.json`` and any tier-4 ``channel=wechat``
bindings, then logs WARNINGS so operators know to re-scan with
``--wechat <agent_id>``.

Tests here cover the two artefacts independently so a regression in
one path doesn't mask a regression in the other.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pip_agent.agent_host import _sweep_legacy_wechat
from pip_agent.routing import Binding, BindingTable


class TestSweepLegacySession:
    def test_legacy_session_file_deleted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        state_dir = tmp_path / ".pip"
        state_dir.mkdir()
        legacy = state_dir / "wechat_session.json"
        legacy.write_text('{"token": "x"}', "utf-8")
        bindings = BindingTable()
        bindings_path = state_dir / "bindings.json"

        with caplog.at_level(logging.WARNING, logger="pip_agent.agent_host"):
            _sweep_legacy_wechat(state_dir, bindings, bindings_path)

        assert not legacy.exists()
        assert any(
            "legacy single-account WeChat session" in rec.message
            for rec in caplog.records
        )

    def test_idempotent_when_no_legacy_artefacts(
        self, tmp_path: Path,
    ) -> None:
        state_dir = tmp_path / ".pip"
        state_dir.mkdir()
        bindings = BindingTable()
        bindings_path = state_dir / "bindings.json"
        # Must not raise, must not create anything, must not save the
        # bindings file (we haven't asked for that).
        _sweep_legacy_wechat(state_dir, bindings, bindings_path)
        assert not bindings_path.exists()


class TestSweepTier4Binding:
    def test_tier4_channel_wechat_binding_removed_and_saved(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        state_dir = tmp_path / ".pip"
        state_dir.mkdir()
        bindings = BindingTable()
        bindings.add(Binding(
            agent_id="pip-boy", tier=4,
            match_key="channel", match_value="wechat",
        ))
        # Keep a non-wechat binding so we can verify only the
        # wechat-scoped one is removed.
        bindings.add(Binding(
            agent_id="stella", tier=4,
            match_key="channel", match_value="wecom",
        ))
        bindings_path = state_dir / "bindings.json"

        with caplog.at_level(logging.WARNING, logger="pip_agent.agent_host"):
            _sweep_legacy_wechat(state_dir, bindings, bindings_path)

        remaining = bindings.list_all()
        assert all(
            not (b.tier == 4 and b.match_key == "channel"
                 and b.match_value == "wechat")
            for b in remaining
        )
        assert any(
            b.agent_id == "stella" for b in remaining
        ), "Non-wechat tier-4 bindings must be preserved"
        # Sweep must persist the change so the next boot sees the
        # cleaned table on disk.
        assert bindings_path.exists()
        assert any(
            "tier-4 channel=wechat" in rec.message
            for rec in caplog.records
        )

    def test_tier3_account_id_bindings_untouched(
        self, tmp_path: Path,
    ) -> None:
        state_dir = tmp_path / ".pip"
        state_dir.mkdir()
        bindings = BindingTable()
        bindings.add(Binding(
            agent_id="pip-boy", tier=3,
            match_key="account_id", match_value="bot-a",
        ))
        bindings_path = state_dir / "bindings.json"
        _sweep_legacy_wechat(state_dir, bindings, bindings_path)
        # Tier-3 is the new world — sweep must not touch it.
        assert len(bindings.list_all()) == 1
        # Nothing changed, so we don't need to have saved either.
        assert not bindings_path.exists()
