"""Lifecycle coordinator for the multi-account WeChat channel.

The :class:`WeChatChannel` itself is a pure transport — it knows how to
log accounts in, poll for inbound, and send replies. The *lifecycle*
bits (spawning poll threads per account, persisting bindings, cancelling
an in-progress QR scan) are cross-cutting concerns that touch the
bindings table, the inbound queue, and the host-wide stop event. Those
belong on the host side.

Rather than scatter those concerns across ``run_host`` boot code and
``host_commands`` handlers, we collect them on one object. ``AgentHost``
holds a reference (optional — ``None`` when WeChat isn't in use) and
forwards it into :class:`CommandContext` so the ``/wechat`` slash
commands can reach it.

Threading model
---------------
* One daemon thread per logged-in account runs ``wechat_poll_loop``.
  Threads are tracked in ``_poll_threads`` so we don't double-spawn if
  the same account is re-added (happens during QR re-login of an
  already-registered bot).
* At most one QR login runs at a time. ``_qr_thread`` / ``_qr_cancel``
  hold the active worker; :meth:`cancel_qr` sets the event so the
  ``WeChatChannel.login`` poll loop exits within ~1 s.
* ``stop_event`` (the host-wide shutdown signal) also aborts any
  in-progress QR login — operators don't want ``/exit`` to be blocked
  behind a 5-minute QR deadline.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from pip_agent.channels.wechat import _wechat_operator_print
from pip_agent.routing import (
    AgentRegistry,
    Binding,
    BindingTable,
    normalize_agent_id,
)

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from pip_agent.channels.base import InboundMessage
    from pip_agent.channels.wechat import WeChatChannel

log = logging.getLogger(__name__)


class WeChatController:
    """Owns multi-account WeChat lifecycle across run_host / slash commands."""

    def __init__(
        self,
        *,
        channel: "WeChatChannel",
        registry: AgentRegistry,
        bindings: BindingTable,
        bindings_path: Path,
        msg_queue: "list[InboundMessage]",
        q_lock: threading.Lock,
        stop_event: threading.Event,
    ) -> None:
        self.channel = channel
        self.registry = registry
        self.bindings = bindings
        self.bindings_path = bindings_path
        self.msg_queue = msg_queue
        self.q_lock = q_lock
        self.stop_event = stop_event

        self._poll_threads: dict[str, threading.Thread] = {}
        self._qr_cancel: threading.Event | None = None
        self._qr_thread: threading.Thread | None = None
        self._qr_agent_id: str = ""
        self._lock = threading.Lock()

    # -------------------------------------------------------------
    # Poll thread lifecycle
    # -------------------------------------------------------------

    def spawn_poll(self, account_id: str) -> bool:
        """Start a poll thread for ``account_id`` if one isn't already running.

        Returns ``True`` if a new thread was started. Silently no-ops if
        the account isn't registered on the channel (caller should
        :meth:`WeChatChannel.add_account` first) or if a thread already
        exists.
        """
        from pip_agent.channels.wechat import wechat_poll_loop

        acc = self.channel.get_account(account_id)
        if acc is None:
            log.warning(
                "WeChatController.spawn_poll: unknown account %s",
                account_id,
            )
            return False
        with self._lock:
            existing = self._poll_threads.get(account_id)
            if existing is not None and existing.is_alive():
                return False
            t = threading.Thread(
                target=wechat_poll_loop, daemon=True,
                name=f"wechat-poll-{account_id}",
                args=(
                    self.channel, account_id,
                    self.msg_queue, self.q_lock, self.stop_event,
                ),
            )
            self._poll_threads[account_id] = t
            t.start()
        return True

    def spawn_polls_for_all_logged_in(self) -> int:
        """Start poll threads for every account that currently has a token.

        Called once at host boot. Accounts without a token (e.g. one
        whose session expired with ``ret=-14``) are skipped; operators
        re-log them in via ``/wechat add``.
        """
        started = 0
        for aid in self.channel.account_ids():
            acc = self.channel.get_account(aid)
            if acc is not None and acc.is_logged_in:
                if self.spawn_poll(aid):
                    started += 1
        return started

    # -------------------------------------------------------------
    # QR login lifecycle
    # -------------------------------------------------------------

    def is_qr_in_progress(self) -> bool:
        with self._lock:
            t = self._qr_thread
            return t is not None and t.is_alive()

    def current_qr_agent(self) -> str:
        """Agent id the in-progress QR scan will bind to (empty if none)."""
        with self._lock:
            if self._qr_thread is not None and self._qr_thread.is_alive():
                return self._qr_agent_id
            return ""

    def cancel_qr(self) -> bool:
        """Signal an in-progress QR login to abort.

        Returns ``True`` if a running login was signalled, ``False`` if
        there wasn't one.
        """
        with self._lock:
            cancel = self._qr_cancel
            t = self._qr_thread
            if cancel is None or t is None or not t.is_alive():
                return False
            cancel.set()
            return True

    def start_qr_login(self, agent_id: str) -> tuple[bool, str]:
        """Kick off a background QR login that binds to ``agent_id`` on success.

        Returns ``(accepted, message)``. ``accepted=False`` means the
        login wasn't started (unknown agent, or another login is
        already in flight); the message explains why.
        """
        aid = normalize_agent_id(agent_id)
        if not self.registry.get_agent(aid):
            return False, f"unknown agent: {agent_id!r}"

        with self._lock:
            if self._qr_thread is not None and self._qr_thread.is_alive():
                return False, (
                    f"another QR login is already in progress "
                    f"(agent={self._qr_agent_id}); /wechat cancel it first"
                )
            cancel = threading.Event()
            t = threading.Thread(
                target=self._qr_worker, daemon=True,
                name=f"wechat-qr-{aid}",
                args=(aid, cancel),
            )
            self._qr_cancel = cancel
            self._qr_thread = t
            self._qr_agent_id = aid
            t.start()
        return True, f"QR scan started — scan with WeChat to bind to agent {aid}"

    def _qr_worker(self, agent_id: str, cancel: threading.Event) -> None:
        """Background worker: run :meth:`WeChatChannel.login` then bind on success."""
        try:
            acc = self.channel.login(self.stop_event, cancel)
        except Exception as exc:  # noqa: BLE001
            log.exception("wechat QR worker crashed")
            _wechat_operator_print(f"  [wechat] QR worker error: {exc}")
            return
        if acc is None:
            # login() already printed the specific reason (expired /
            # cancelled / timeout / transport error). Nothing to add.
            return

        # Register + persist credential file.
        self.channel.add_account(acc)

        # Upsert tier-3 binding. ``remove`` first is idempotent; it
        # covers the case where the user re-scans the same bot to move
        # it to a different agent.
        self.bindings.remove("account_id", acc.account_id)
        self.bindings.add(Binding(
            agent_id=agent_id, tier=3,
            match_key="account_id", match_value=acc.account_id,
        ))
        try:
            self.bindings.save(self.bindings_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("wechat QR worker: bindings.save failed")
            _wechat_operator_print(f"  [wechat] Binding save failed: {exc}")
            return

        # Start the poll loop so the new bot actually receives messages.
        self.spawn_poll(acc.account_id)
        _wechat_operator_print(
            f"  [wechat] Bound account {acc.account_id} -> agent:{agent_id}"
            " and polling started.",
        )

    # -------------------------------------------------------------
    # Removal
    # -------------------------------------------------------------

    def remove_account(self, account_id: str) -> bool:
        """Stop polling + delete creds + drop tier-3 binding for ``account_id``.

        Returns ``True`` if any of (account, binding) were removed.
        """
        # Removing from the channel first means the poll loop's next
        # ``get_account`` returns ``None`` and the loop exits on its own
        # — cleaner than trying to signal via stop_event (which would
        # also kill the other accounts' threads).
        removed_account = self.channel.remove_account(account_id)

        had_binding = self.bindings.remove("account_id", account_id)
        if had_binding:
            try:
                self.bindings.save(self.bindings_path)
            except Exception as exc:  # noqa: BLE001
                log.exception("wechat: bindings.save failed during remove")
                _wechat_operator_print(f"  [wechat] Binding save failed: {exc}")

        with self._lock:
            self._poll_threads.pop(account_id, None)

        return removed_account or had_binding

    # -------------------------------------------------------------
    # Introspection for /wechat list
    # -------------------------------------------------------------

    def list_accounts(self) -> list[dict[str, str]]:
        """Snapshot of every registered account with its binding (if any).

        Each entry: ``{"account_id": ..., "agent_id": ..., "logged_in": "yes"/"no"}``.
        ``agent_id`` is empty if no tier-3 binding points at this account.
        """
        binding_map: dict[str, str] = {}
        for b in self.bindings.list_all():
            if b.tier == 3 and b.match_key == "account_id":
                binding_map[b.match_value] = b.agent_id

        out: list[dict[str, str]] = []
        for aid in self.channel.account_ids():
            acc = self.channel.get_account(aid)
            logged = "yes" if (acc is not None and acc.is_logged_in) else "no"
            out.append({
                "account_id": aid,
                "agent_id": binding_map.get(aid, ""),
                "logged_in": logged,
            })
        return out


__all__ = ["WeChatController"]
