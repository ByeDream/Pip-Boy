# Pip-Boy Performance Baseline

Snapshot of Pip-Boy's end-to-end cost distribution as of version `0.4.3`.
Purpose: give any future contributor a quick mental model of where time and
money go, what has already been pushed to its floor, and what remains
worth chasing.

All numbers below are measured with the in-tree profiler
(`src/pip_agent/_profile.py`, emits JSONL under `<WORKDIR>/profile-logs/profile.jsonl`).
Source of truth is the raw data; this document is a guide, not a contract.

## 1. Cold start

Measured from process launch to `cold_start.loop_ready` (host event loop
able to accept inbounds). Run mode `auto` (CLI + WeChat + WeCom).

| Milestone                    | Typical    | Nature              |
| ---------------------------- | ---------- | ------------------- |
| `cold_start.logging_ready`   | ~0 ms      |                     |
| `cold_start.run_host_imported` | ~245 ms  | Lazy top-level imports |
| `cold_start.registry_ready`  | +5–40 ms   | Agent registry      |
| `cold_start.wechat_instance_ready` | +130 ms | `pywinauto` attach to WeChat window |
| `cold_start.wecom_import_done` | +390–400 ms | `aibot` SDK import (WeCom) |
| `cold_start.loop_ready`      | **~780–830 ms** total | Ready for traffic   |

The two observable bottlenecks — `pywinauto` attach (~130 ms) and
`aibot` import (~400 ms) — are **external library costs**, not ours.
Both are deferred until the respective channel is enabled. Since
channel enablement is now on-demand (WeCom only when
`WECOM_BOT_ID`/`WECOM_BOT_SECRET` are present, WeChat only when
`credentials/wechat/*.json` exists or `--wechat <agent_id>` is passed),
a CLI-only boot pays neither cost.

See `cold_start.*` events in any profile for the exact breakdown on a
given run.

## 2. Warm per-turn path

For a session that has already opened a streaming `ClaudeSDKClient`:

| Metric                            | Typical   | Notes                         |
| --------------------------------- | --------- | ----------------------------- |
| `host.lock_wait_{start,end}` Δ    | 0 ms      | Global semaphore is no longer on this path (see `agent_host.py`: `_one_shot_semaphore` only guards non-streaming one-shot calls). |
| `stream.session_init` (warm)      | 2–7 ms    | Reused client ready to push user message. |
| `stream.user_pushed` → `stream.first_text` (LLM TTFT) | **2.6–6 s typical, up to 10 s for tool-using turns** | Dominated by Anthropic-side latency. |
| `stream.first_text` → `stream.result` | 50–200 ms | Streaming tail + cost accounting. |
| Full turn wall clock              | ~3–6 s / turn (text-only) | First-text latency + short tail. |

**The warm session-init (2–7 ms) is three orders of magnitude below
the legacy per-turn cost (~1.3 s) and is near a physical floor; further
optimization here is not worth pursuing.**

## 3. LLM-side cost distribution (what dominates wall clock)

From a ~7 minute real WeCom conversation (21 LLM turns, no tool-heavy
usage):

- Median TTFT: ~4–5 s
- p90 TTFT: ~8 s
- Max TTFT (tool-using turn with 2 tool rounds): ~10 s
- Tool round overhead: ~3 s per additional round (normal for Claude
  multi-turn tool-use)
- Output stream duration: 50 ms – 1 s depending on reply length

Attribution: **TTFT is set almost entirely by Anthropic** — our code
contributes well under 10 ms between receiving the inbound and pushing
the user message. Shortening TTFT requires either:
(a) reducing `system_prompt_append` (affects agent quality),
(b) switching models (affects quality),
(c) enabling prompt caching end-to-end (see §6 — open item).

## 4. Cost per turn (input-token dynamics)

Typical Pip agent turn with `claude_code` preset + Pip persona
`system_prompt_append`:

| Component          | Size per turn          |
| ------------------ | ---------------------- |
| System prompt prefix (preset + append) | ~30–32 K tokens |
| Conversation history | grows 100–500 tokens per prior turn |
| Current user message | ~1–100 tokens |
| Output (reply)     | 10–300 tokens typical, up to ~1000 on detail-rich replies |

Observed single-turn cost in a cold (non-cached) session grows from
~$0.20 on turn 1 to ~$2+ on turns 15+ in a running conversation
(conversation history compounding; the ~30 K system prompt prefix is
paid again every turn).

**Prompt caching is the single largest unexploited lever** — see §6.

## 5. Message-arrival dynamics (real user)

From the same real WeCom session (25 inbound messages):

| Inter-arrival gap | Value    |
| ----------------- | -------- |
| min               | 1.4 s    |
| p25               | 5.2 s    |
| **p50**           | **16.7 s** |
| p75               | 24 s     |
| p90               | 32 s     |
| max               | 50 s     |

Users think between messages. The Tier 2 **lock-time coalescing**
(`host.redirected_to_lock_batch`, `host.batch_coalesced`) captures
messages that arrive while a prior turn is still running, and fuses
them if 2+ accumulate. In real traffic the fusion rate is low
(~1 fusion per 25 messages) because real inter-arrival gaps exceed
typical turn duration. This is a correctness mechanism with occasional
savings, **not a primary cost driver** — do not expect dramatic wins
from further tuning here.

An **"ingress debounce"** window before starting a fresh turn was
considered and rejected: the data showed that even a 1000 ms debounce
catches 0 escaped messages, and a 5000 ms window (unusable UX-wise)
would catch only 33 %. See git history around this doc's creation for
the full analysis.

## 6. Open item: Anthropic prompt caching

**Status: not reliably hitting.** Investigated and intentionally
deferred.

The `stream.result` profile event now carries `input_tokens`,
`output_tokens`, `cache_read`, and `cache_creation` fields. A short
probe showed a "jumping" hit pattern: some turns hit the cache at
~99 % (e.g. a single turn reused 31 K prior tokens for a fraction of
a cent), while neighboring turns missed entirely and paid full
cache-creation cost (~30 K tokens each at a 25 % premium).

The likely root cause is that the `claude_code` preset embeds
dynamic sections (cwd, git status, auto-memory, timestamps) that shift
between turns, breaking the byte-stable prefix that prompt caching
requires.

Anthropic's SDK exposes `exclude_dynamic_sections: True` on the system
prompt preset (`claude_agent_sdk/types.py::SystemPromptPreset`), which
is designed precisely for this. A point-in-time experiment (see
`scripts/_analyze_real_gaps.py` and the diagnostic profile event
schema) confirmed:

- With the flag, cache-read did fire correctly (turn 2 read ~30 K
  cached tokens).
- **But** the currently bundled `claude.exe` (inside `claude_agent_sdk/_bundled/`)
  exhibited multi-minute TTFTs on the first two turns when the flag
  was enabled — either a CLI version mismatch or a bug in the bundled
  binary.

Decision: revert, instrument, document. When the bundled CLI is
upgraded (via `claude_agent_sdk` package update), re-enable the flag
at both call sites (`streaming_session.py` and `agent_runner.py`) and
confirm cache hit rate via the `cache_read / (cache_read + cache_creation + input_tokens)` ratio in `stream.result` events.

Potential savings when this lands: on long sessions the ~30 K system
prompt prefix could move from paid-every-turn (~$0.11/turn) to
paid-once (~$0.009/turn read cost), roughly 85 % reduction of input-token
cost on long conversations.

## 7. Things already at the floor — don't re-optimize

These were aggressively tuned in earlier work and further effort
has diminishing or negative returns:

- Cold start is 780–830 ms and two remaining large items are external
  imports; more trimming requires not importing those libraries, which
  means disabling the corresponding channels.
- Warm session init is 2–7 ms; further reduction would require
  skipping SDK handshake, which is neither safe nor meaningful.
- Concurrency model (`_session_locks` + per-session streaming +
  `_one_shot_semaphore` guarding only the one-shot path) is
  architecturally correct and free of global contention. See
  `tests/test_agent_host_concurrency.py`.
- Tier 2 lock-time coalescing mechanism is in place and verified; its
  fusion rate is set by human traffic patterns, not by tuning knobs.

## 8. Tools for future investigation

| Tool                                 | Purpose                                         |
| ------------------------------------ | ----------------------------------------------- |
| `src/pip_agent/_profile.py`          | Emit `span` / `event` JSONL under `<WORKDIR>/profile-logs/profile.jsonl`. |
| `scripts/analyze_smoke.py`           | Rollup summary of cold start, Tier 2 batching, per-turn timing. |
| `scripts/_analyze_real_gaps.py`      | Inter-arrival gap distribution and pre-flight debounce effectiveness analysis. |

Read any of those profile events directly (the schema is stable and
self-describing via the `evt` / `meta` fields). No binary format, no
decoder required.
