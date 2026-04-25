"""Regression tests for the multi-account WeChat refactor.

Scope:

* ``_WeChatAccount`` round-trips through the per-account credential
  file layout (``credentials/wechat/<id>.json``).
* ``WeChatController.spawn_polls_for_all_logged_in`` starts one poll
  thread per account and inbound messages route through tier-3
  ``account_id`` bindings back to distinct agents.
* ``WeChatController.start_qr_login`` refuses an unknown ``agent_id``
  without mutating any state.
* ``WeChatController.remove_account`` drops creds, the registration,
  and the tier-3 binding.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from pip_agent.channels.wechat import WeChatChannel, _WeChatAccount
from pip_agent.routing import AgentConfig, Binding, BindingTable
from pip_agent.wechat_controller import WeChatController


def _make_channel_with_accounts(
    tmp_path: Path, account_ids: list[str],
) -> WeChatChannel:
    ch = WeChatChannel(tmp_path / ".pip")
    for aid in account_ids:
        acc = _WeChatAccount(
            account_id=aid,
            bot_token=f"token-{aid}",
            base_url="https://ilinkai.weixin.qq.com",
        )
        ch.add_account(acc)
    return ch


class _StubRegistry:
    """Only the methods :class:`WeChatController` actually reaches for."""

    def __init__(self, known: set[str]) -> None:
        self._known = known

    def get_agent(self, agent_id: str) -> AgentConfig | None:
        if agent_id in self._known:
            return AgentConfig(id=agent_id, name=agent_id, model="stub")
        return None


# ---------------------------------------------------------------------------
# _WeChatAccount on-disk round trip
# ---------------------------------------------------------------------------


class TestAccountPersistence:
    def test_to_from_file_round_trip(self, tmp_path: Path) -> None:
        p = tmp_path / "bot-a.json"
        acc = _WeChatAccount(
            account_id="bot-a",
            bot_token="tok",
            base_url="https://ilinkai.weixin.qq.com",
            user_id="uid",
            get_updates_buf="buf",
            cred_path=p,
        )
        acc.save()
        loaded = _WeChatAccount.from_file(p)
        assert loaded is not None
        assert loaded.account_id == "bot-a"
        assert loaded.bot_token == "tok"
        assert loaded.get_updates_buf == "buf"
        assert loaded.is_logged_in is True

    def test_missing_account_id_is_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "corrupt.json"
        p.write_text('{"token": "x"}', "utf-8")
        assert _WeChatAccount.from_file(p) is None


# ---------------------------------------------------------------------------
# Per-account poll routing
# ---------------------------------------------------------------------------


class TestMultiAccountPollRouting:
    """End-to-end: two accounts, two agents, one binding per account.

    We don't actually start network threads — we drive ``wechat_poll_loop``
    via the real ``WeChatChannel.poll`` path with a mocked HTTP client and
    observe that each inbound carries the originating ``account_id`` and
    therefore resolves to a different agent via tier-3 bindings.
    """

    def test_tier3_binding_resolves_per_account(self, tmp_path: Path) -> None:
        ch = _make_channel_with_accounts(tmp_path, ["bot-a", "bot-b"])

        # Mock each account's poll to return one text message with that
        # account's sender so we can trace attribution.
        def _make_response(from_user: str, seq: int) -> MagicMock:
            resp = MagicMock()
            resp.json.return_value = {
                "ret": 0,
                "msgs": [
                    {
                        "seq": seq,
                        "message_type": 1,
                        "message_state": 2,
                        "from_user_id": from_user,
                        "context_token": f"ctx-{from_user}",
                        "item_list": [
                            {"type": 1, "text_item": {"text": "hi"}},
                        ],
                    },
                ],
                "get_updates_buf": "",
            }
            return resp

        responses = {
            "bot-a": _make_response("userA", 1),
            "bot-b": _make_response("userB", 2),
        }

        def _fake_post(url: str, *a, **kw):  # noqa: ANN001
            # The channel's ``poll_inner`` sets ``base_url`` the same
            # for both; we demux by the bot token instead.
            auth = (kw.get("headers") or {}).get("Authorization", "")
            if "token-bot-a" in auth:
                return responses["bot-a"]
            if "token-bot-b" in auth:
                return responses["bot-b"]
            raise AssertionError(f"unexpected auth header: {auth!r}")

        with patch.object(ch._http, "post", side_effect=_fake_post):
            msgs_a = ch.poll("bot-a")
            msgs_b = ch.poll("bot-b")

        assert len(msgs_a) == 1 and msgs_a[0].account_id == "bot-a"
        assert len(msgs_b) == 1 and msgs_b[0].account_id == "bot-b"

        # Now wire tier-3 bindings and check routing resolves to
        # different agents for the two accounts.
        bindings = BindingTable()
        bindings.add(Binding(
            agent_id="agent-a", tier=3,
            match_key="account_id", match_value="bot-a",
        ))
        bindings.add(Binding(
            agent_id="agent-b", tier=3,
            match_key="account_id", match_value="bot-b",
        ))

        aid_a, _ = bindings.resolve(
            channel="wechat", account_id=msgs_a[0].account_id,
            peer_id=msgs_a[0].peer_id,
        )
        aid_b, _ = bindings.resolve(
            channel="wechat", account_id=msgs_b[0].account_id,
            peer_id=msgs_b[0].peer_id,
        )
        assert aid_a == "agent-a"
        assert aid_b == "agent-b"


# ---------------------------------------------------------------------------
# WeChatController
# ---------------------------------------------------------------------------


class TestWeChatControllerLifecycle:
    def _mk(
        self, tmp_path: Path, known_agents: set[str] | None = None,
    ) -> tuple[WeChatController, WeChatChannel, BindingTable, Path]:
        ch = _make_channel_with_accounts(tmp_path, ["bot-a"])
        bindings = BindingTable()
        bindings_path = tmp_path / ".pip" / "bindings.json"
        reg = _StubRegistry(known=known_agents or {"pip-boy"})
        ctrl = WeChatController(
            channel=ch,
            registry=reg,  # type: ignore[arg-type]
            bindings=bindings,
            bindings_path=bindings_path,
            msg_queue=[],
            q_lock=threading.Lock(),
            stop_event=threading.Event(),
        )
        return ctrl, ch, bindings, bindings_path

    def test_unknown_agent_refused_without_state_mutation(
        self, tmp_path: Path,
    ) -> None:
        ctrl, _, bindings, _ = self._mk(tmp_path, known_agents={"pip-boy"})
        accepted, message = ctrl.start_qr_login("not-a-real-agent")
        assert accepted is False
        assert "unknown" in message.lower()
        assert not bindings.list_all()
        assert not ctrl.is_qr_in_progress()

    def test_remove_account_drops_creds_and_binding(
        self, tmp_path: Path,
    ) -> None:
        ctrl, ch, bindings, bindings_path = self._mk(tmp_path)
        bindings.add(Binding(
            agent_id="pip-boy", tier=3,
            match_key="account_id", match_value="bot-a",
        ))
        assert ch.get_account("bot-a") is not None

        removed = ctrl.remove_account("bot-a")
        assert removed is True
        assert ch.get_account("bot-a") is None
        assert not any(
            b.match_key == "account_id" and b.match_value == "bot-a"
            for b in bindings.list_all()
        )
        assert not (
            tmp_path / ".pip" / "credentials" / "wechat" / "bot-a.json"
        ).exists()

    def test_list_accounts_reports_logged_in_and_binding(
        self, tmp_path: Path,
    ) -> None:
        ctrl, ch, bindings, _ = self._mk(tmp_path)
        bindings.add(Binding(
            agent_id="pip-boy", tier=3,
            match_key="account_id", match_value="bot-a",
        ))
        rows = ctrl.list_accounts()
        assert rows == [
            {"account_id": "bot-a", "agent_id": "pip-boy", "logged_in": "yes"},
        ]

    def test_second_start_qr_login_rejected_while_running(
        self, tmp_path: Path,
    ) -> None:
        ctrl, ch, _, _ = self._mk(tmp_path)
        # Make ``channel.login`` block until we let it go, so the first
        # ``start_qr_login`` keeps the QR thread alive long enough for
        # the second call to observe it.
        blocker = threading.Event()

        def _blocking_login(stop, cancel, **kw):  # noqa: ANN001
            blocker.wait(timeout=2.0)
            return None

        with patch.object(ch, "login", side_effect=_blocking_login):
            accepted_1, _ = ctrl.start_qr_login("pip-boy")
            assert accepted_1 is True
            # Poll a few times since the worker thread has to actually
            # enter ``login`` before ``is_qr_in_progress`` flips.
            for _ in range(50):
                if ctrl.is_qr_in_progress():
                    break
                time.sleep(0.01)
            assert ctrl.is_qr_in_progress()
            accepted_2, msg2 = ctrl.start_qr_login("pip-boy")
            assert accepted_2 is False
            assert "in progress" in msg2.lower()
            blocker.set()
