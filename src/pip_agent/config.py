"""Pip-Boy host-level configuration.

All settings are host concerns only. Tool credentials, model routing, and
permission settings are handled by Claude Code itself via `.claude/settings.json`
and env vars — Pip-Boy does not proxy them.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# pydantic-settings reads ``.env`` into the Settings model but does NOT
# push values into ``os.environ``. Tools that bypass Settings and call
# ``os.getenv`` directly (e.g. ``pip_agent.web.search_web`` reading
# ``TAVILY_API_KEY``) would then see nothing. Priming ``os.environ`` here
# — before Settings instantiation below — makes ``.env`` the single source
# of truth for both access patterns. ``override=False`` preserves any
# value the operator exported in their shell.
load_dotenv(override=False)

# Pip-Boy exposes ``ANTHROPIC_API_KEY`` as the *only* user-facing credential
# variable; the bearer-vs-x-api-key choice is an internal detail driven by
# ``ANTHROPIC_BASE_URL`` (see ``pip_agent.anthropic_client``). The Anthropic
# Python SDK, however, auto-reads ``ANTHROPIC_AUTH_TOKEN`` from the process
# environment and silently *prefers* it over ``ANTHROPIC_API_KEY``. A stale
# ``AUTH_TOKEN`` left in the operator's shell (from old Claude Code setups
# or another tool) would therefore hijack the key the user just put in
# ``.env`` with no warning. Scrub it here, before any SDK or subprocess sees
# the env, so the single-credential contract actually holds.
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR: Path = Path.cwd()
"""Absolute path of the workspace Pip-Boy is running in.

Captured once at import. All per-agent subdirectories live under ``WORKDIR/.pip/``.
"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The single Anthropic credential. The header it goes out as
    # (``x-api-key`` vs ``Authorization: Bearer``) is decided by whether
    # ``anthropic_base_url`` is set — see ``pip_agent.anthropic_client``.
    # Falls back to ``os.environ`` for users who export it from their shell
    # rather than ``.env``.
    anthropic_api_key: str = Field(default="")
    anthropic_base_url: str = Field(default="")

    # Three model tiers, ordered strongest to cheapest. Pip-Boy never uses
    # a concrete model name at a call site — every site picks a tier
    # (t0/t1/t2) and resolves through :mod:`pip_agent.models`. Async /
    # background tasks (heartbeat, cron, reflect, dream) are pinned to
    # fixed tiers in code; persona-driven turns pick their tier from
    # ``persona.md``. Failures degrade DOWN the chain (t0 -> t1 -> t2);
    # never up.
    model_t0: str = Field(default="")
    model_t1: str = Field(default="")
    model_t2: str = Field(default="")

    # Controls *only* the logging threshold — see
    # ``pip_agent.__main__._configure_logging``. Streaming agent replies
    # and ``[tool: ...]`` traces are part of the interactive CLI contract
    # and are NOT gated by this flag. Flip to ``true`` when you need the
    # internal log firehose (scheduler ticks, memory pipeline, SDK init).
    verbose: bool = Field(default=False)

    # Structured timing profiler — see ``pip_agent._profile``. When
    # enabled, every inbound turn emits JSONL span / event records to
    # ``profile_dir / profile.jsonl`` (append-only, thread + coroutine
    # safe). Default **off**: hot-path cost when disabled is a single
    # attribute check, so leaving the instrumentation in production is
    # safe, but the file it writes is intended for perf investigations,
    # not operational logging.
    #
    # ``PIP_PROFILE=1`` in the shell env is also honoured as a one-off
    # override when you don't want to touch ``.env``.
    enable_profiler: bool = Field(default=False)

    # Where profile JSONL lands. Empty string → the profiler picks a
    # default under ``D:\Workspace\pip-test\profile-logs``. Override via
    # ``PIP_PROFILE_DIR`` env var still works.
    profile_dir: str = Field(default="")

    # Tier 1 streaming session reuse. When enabled, AgentHost keeps one
    # ``ClaudeSDKClient`` alive per session_key, so subsequent turns on
    # the same session avoid the ~400 ms subprocess spawn + handshake.
    # Ephemeral senders (cron / heartbeat) still go through the one-shot
    # ``run_query`` path — mixing stateful and stateless flows in the
    # same client would pollute the user's transcript. Disable to force
    # every turn back to the one-shot path (useful for A/B comparison).
    enable_streaming_session: bool = Field(default=True)

    # When True, register our own web_search / web_fetch MCP tools and
    # disable Claude Code's native WebSearch / WebFetch. Some upstream
    # proxies reject the native tool schema. Set to False to use CC's
    # built-in web tools when the upstream supports them.
    use_custom_web_tools: bool = Field(default=True)

    # Interval (seconds) between ASCII art animation frame advances.
    # Set to 0 to disable animation (single-frame themes are unaffected).
    art_anim_interval: float = Field(default=2.0)

    # Idle-eviction window for cached streaming clients, in seconds.
    # Sweep runs on the host scheduler; clients idle longer than this
    # are disconnected to (a) free ~25 MB RSS per stale ``claude.exe``
    # subprocess, and (b) let the next turn pick up refreshed memory
    # enrichment (``system_prompt_append`` is frozen at connect()).
    # 180 s is slightly above the typical "user replies within 3 min"
    # window from perf-report-new.md; tune per workload.
    stream_idle_ttl_sec: int = Field(default=180)

    # Hard cap on concurrent live streaming clients. Acts as the new
    # analogue of the old per-turn ``Semaphore(3)``: still bounds total
    # resident ``claude.exe`` processes, but now the bound is per
    # *session* rather than per *turn*. 10 is comfortable for the 4-5
    # active peers observed in perf logs; raise if you run a larger
    # agent fleet.
    stream_max_live: int = Field(default=10)

    # Tier 2: when multiple text-only inbound messages from the same
    # sender/peer land in the SAME drain tick (≤ ``0.3 s`` apart with
    # the current inbound loop cadence), fuse them into a single LLM
    # turn. Typical win is on WeCom, where users tend to break a
    # thought across 2-3 bubbles (``"早"`` / ``"今天提醒我开会"`` /
    # ``"谢谢"``). Each bubble previously spent a full session_init
    # recap + LLM round trip; coalescing cuts that to one.
    #
    # Messages are eligible only if ALL of these hold:
    #
    # * ``attachments`` is empty (no images / files / voice — media
    #   changes the prompt shape and we don't want to reorder image
    #   placement just to save tokens).
    # * ``source_job_id`` is empty (not a heartbeat / cron payload;
    #   those are already coalesced by ``HostScheduler`` and batching
    #   them with a human message would misleadingly attribute the
    #   keepalive to the user).
    # * ``text`` does not start with ``"/"`` (host commands — ``/exit``,
    #   ``/flush``, …— must stay as discrete turns).
    #
    # Disable to force one-inbound-per-turn behaviour (useful when
    # debugging agent logic that depends on seeing messages
    # individually).
    batch_text_inbounds: bool = Field(default=True)

    # Joiner inserted between coalesced message bodies. Two newlines
    # preserves paragraph semantics for the model (each bubble reads as
    # its own paragraph). Keep this narrow — changing it is observable
    # in the conversation transcript.
    batch_text_joiner: str = Field(default="\n\n")

    # Idle backoff for the WeChat iLink ``getupdates`` long-poll.
    #
    # Observed problem: the iLink server frequently fast-returns from
    # ``getupdates`` with no messages (response < 50 ms). Without any
    # client-side wait between calls, the poll loop hammers the server
    # at ~20 req/sec when the user's WeChat is idle — burning CPU,
    # local HTTP overhead, and the server's rate budget, and flooding
    # verbose-mode logs with httpx INFO lines.
    #
    # Contract: only applied when the previous poll returned ZERO new
    # messages AND no transport error fired. If messages arrived, we
    # loop back immediately (active-conversation latency matters more
    # than idle thrift). Error backoff is a separate, stronger scale
    # (``2s * consecutive_errors`` capped at 30 s, see
    # :func:`wechat_poll_loop`).
    #
    # 1.0 s balances:
    #   * interactive latency on a quiet channel (one user types, we
    #     fetch within ≤1 s of their send)
    #   * ~20x reduction in idle-state request volume (was ~20 req/s,
    #     now ~1 req/s)
    # Set to 0 to restore the pre-Tier-2 hot-loop behaviour.
    wechat_poll_idle_sec: float = Field(default=1.0)

    wecom_bot_id: str = Field(default="")
    wecom_bot_secret: str = Field(default="")

    # Heartbeat injection timing. ``HEARTBEAT.md`` at each agent's
    # ``.pip/`` is fired as a ``<heartbeat>`` inbound every
    # ``heartbeat_interval`` seconds during the active window. Set the interval
    # to 0 to disable. Heartbeat is NOT part of the memory pipeline — reflect
    # triggers are PreCompact + ``/exit`` only.
    heartbeat_interval: int = Field(default=1800)
    heartbeat_active_start: int = Field(default=9)
    heartbeat_active_end: int = Field(default=22)

    # Dream trigger — L2 consolidate + L3 axiom distillation.
    #
    # Dream runs periodically against already-persisted observations (reflect
    # is upstream and writes them from PreCompact / /exit). The trigger
    # conditions, ALL required:
    #
    # 1. Clock is within ``[dream_hour_start, dream_hour_end)`` — local time,
    #    not UTC. Default 2 am – 5 am: idle for most users, lowest
    #    contention on shared-machine setups, and Anthropic pricing tiers
    #    occasionally soften overnight.
    # 2. ``len(observations.jsonl) >= dream_min_observations`` — don't
    #    consolidate over a near-empty pile; let signal accumulate first.
    # 3. Last user / channel activity was at least ``dream_inactive_minutes``
    #    ago — don't collide with an active conversation; the consolidate
    #    pass holds the memory store for a non-trivial window.
    #
    # Set ``dream_min_observations`` to 0 to fire on every idle window.
    # Set ``dream_inactive_minutes`` to 0 to skip the idle gate.
    # Set ``dream_hour_start == dream_hour_end`` to disable Dream entirely.
    dream_hour_start: int = Field(default=2)
    dream_hour_end: int = Field(default=5)
    dream_min_observations: int = Field(default=20)
    dream_inactive_minutes: int = Field(default=30)

    # Plugin marketplaces auto-registered at host cold-start. Comma-separated
    # ``owner/repo`` (or any spec ``claude plugin marketplace add`` accepts).
    # Empty string disables the bootstrap entirely. The default points at
    # Anthropic's curated catalogue so a fresh checkout already has things
    # like ``exa`` / ``firecrawl`` / ``brightdata-plugin`` discoverable via
    # ``/plugin search`` without a manual ``marketplace add`` step.
    #
    # Idempotent: existing marketplaces are detected via ``marketplace list
    # --json`` and skipped, so this fires at most one network clone per spec
    # over the host's lifetime. Failures (offline, proxy down, malformed
    # repo) are logged at WARNING and do NOT block startup — the rest of
    # the host boots normally; the user just won't see those plugins in
    # ``/plugin search`` until they retry.
    bootstrap_marketplaces: str = Field(
        default="anthropics/claude-plugins-official",
    )

    # Timeout (seconds) for plugin / marketplace operations that go to
    # the network. Covers ``marketplace add`` / ``marketplace update``
    # (git clone) and ``plugin install`` (clone + ``npm`` / ``uv``
    # dependency fetch). Local-only operations (``list``, ``search``,
    # ``uninstall``, ``enable``, ``disable``) keep the static 30 s cap
    # in :mod:`pip_agent.plugins`.
    #
    # The 180 s default is the "would have succeeded if we'd waited"
    # threshold observed for the bundled ``exa`` plugin install on a
    # proxied connection — the original 30 s ceiling killed that one
    # install at ~28 s. Bump higher (e.g. 600) for very slow proxies;
    # lower if you'd rather fail fast and retry.
    plugin_network_timeout_sec: float = Field(default=180.0)

    def check_required(self) -> None:
        """Host-level credential check.

        Pip-Boy forwards ``ANTHROPIC_API_KEY`` to the Claude Code CLI
        subprocess when set (translated internally to ``ANTHROPIC_AUTH_TOKEN``
        under a proxy so CC sends bearer auth — transparent to the user).
        If nothing is set, CC falls back to its own auth (``claude login`` /
        system config), which is fine — we only surface a warning, never
        fail.
        """
        return None


settings = Settings()

# Runtime flag set by ``run_host(headless=True)``. NOT a pydantic field —
# ``--headless`` is a CLI concern, not a ``.env`` knob. Read by
# ``agent_runner._builtin_disallowed_tools`` to drop ``AskUserQuestion``
# when no TUI is available to render its structured-options UI. Plan mode
# (``EnterPlanMode`` / ``ExitPlanMode``) and ``TodoWrite`` stay enabled
# in headless — the former is forwarded to remote channels via
# :class:`pip_agent.channels.plan_forwarder.PlanForwarder`; the latter is
# a model-internal scaffold that doesn't need a UI to be useful.
headless: bool = False
