"""Three-layer retry onion for Anthropic API calls.

Architecture (adapted from claw0 s09_resilience):

    Layer 1 -- Auth rotation: iterate through non-cooldown AuthProfiles.
    Layer 2 -- Overflow recovery: on context overflow, compact messages
               in-place and retry with the same profile.
    Layer 3 -- Tool-use loop: lives in agent.py; each iteration calls
               ResilienceRunner.call() for a single messages.create.

If all profiles are exhausted, ResilienceRunner falls back through the
configured fallback_models chain. Only when that also fails does it
raise ResilienceExhausted.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import anthropic

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_RETRY = 24
PER_PROFILE = 8
MAX_OVERFLOW_COMPACTION = 3

_COOLDOWN_AUTH = 300.0
_COOLDOWN_BILLING = 300.0
_COOLDOWN_RATE = 120.0
_COOLDOWN_TIMEOUT = 60.0
_COOLDOWN_UNKNOWN = 120.0
_COOLDOWN_OVERFLOW_EXHAUSTED = 600.0



# ---------------------------------------------------------------------------
# 1. FailoverReason + classifier
# ---------------------------------------------------------------------------


class FailoverReason(Enum):
    rate_limit = "rate_limit"
    auth = "auth"
    timeout = "timeout"
    billing = "billing"
    overflow = "overflow"
    unknown = "unknown"


def classify_failure(exc: Exception) -> FailoverReason:
    """Route an exception to a FailoverReason bucket.

    Prefers anthropic exception types and HTTP status codes when available,
    then falls back to string pattern matching.
    """
    if isinstance(exc, anthropic.RateLimitError):
        return FailoverReason.rate_limit
    if isinstance(exc, anthropic.AuthenticationError):
        return FailoverReason.auth
    if isinstance(exc, anthropic.APITimeoutError):
        return FailoverReason.timeout
    if isinstance(exc, anthropic.PermissionDeniedError):
        msg = str(exc).lower()
        if "billing" in msg or "credit" in msg or "payment" in msg:
            return FailoverReason.billing
        return FailoverReason.unknown
    if isinstance(exc, anthropic.BadRequestError):
        m = str(exc).lower()
        if (
            "too long" in m
            or "context" in m
            or "overflow" in m
            or ("token" in m and ("limit" in m or "exceed" in m))
        ):
            return FailoverReason.overflow

    status = getattr(exc, "status_code", None)
    if status == 429:
        return FailoverReason.rate_limit
    if status == 401:
        return FailoverReason.auth
    if status in (402, 403):
        return FailoverReason.billing

    m = str(exc).lower()
    if "rate" in m or "429" in m:
        return FailoverReason.rate_limit
    if "invalid api key" in m or "unauthorized" in m or "401" in m:
        return FailoverReason.auth
    if "timeout" in m or "timed out" in m:
        return FailoverReason.timeout
    if "billing" in m or "quota" in m or "402" in m:
        return FailoverReason.billing
    if (
        "too long" in m
        or "overflow" in m
        or ("context" in m and "limit" in m)
        or ("token" in m and "limit" in m)
    ):
        return FailoverReason.overflow
    return FailoverReason.unknown


_COOLDOWN_BY_REASON: dict[FailoverReason, float] = {
    FailoverReason.auth: _COOLDOWN_AUTH,
    FailoverReason.billing: _COOLDOWN_BILLING,
    FailoverReason.rate_limit: _COOLDOWN_RATE,
    FailoverReason.timeout: _COOLDOWN_TIMEOUT,
}


def _classify_and_mark(
    profile_manager: ProfileManager,
    profile: object,
    exc: Exception,
) -> FailoverReason:
    """Classify *exc*, mark the profile, and return the reason."""
    reason = classify_failure(exc)
    cd = _COOLDOWN_BY_REASON.get(reason, _COOLDOWN_UNKNOWN)
    profile_manager.mark_failure(profile, reason, cd)
    return reason


# ---------------------------------------------------------------------------
# 2. AuthProfile + ProfileManager
# ---------------------------------------------------------------------------


@dataclass
class AuthProfile:
    name: str
    api_key: str
    base_url: str = ""
    cooldown_until: float = 0.0
    failure_reason: str | None = None
    last_good_at: float = 0.0


class ProfileManager:
    def __init__(self, profiles: list[AuthProfile]) -> None:
        self.profiles: list[AuthProfile] = list(profiles)
        self._clients: dict[str, anthropic.Anthropic] = {}

    def select_profile(self) -> AuthProfile | None:
        now = time.time()
        for p in self.profiles:
            if now >= p.cooldown_until:
                return p
        return None

    def select_all_available(self) -> list[AuthProfile]:
        now = time.time()
        return [p for p in self.profiles if now >= p.cooldown_until]

    def mark_failure(
        self,
        profile: AuthProfile,
        reason: FailoverReason,
        cooldown_seconds: float,
    ) -> None:
        profile.cooldown_until = time.time() + cooldown_seconds
        profile.failure_reason = reason.value

    def mark_success(self, profile: AuthProfile) -> None:
        profile.failure_reason = None
        profile.last_good_at = time.time()

    def client_for(self, profile: AuthProfile) -> anthropic.Anthropic:
        """Return a cached Anthropic client bound to this profile's credentials."""
        cached = self._clients.get(profile.name)
        if cached is not None:
            return cached
        kwargs: dict[str, Any] = {"api_key": profile.api_key}
        if profile.base_url:
            kwargs["base_url"] = profile.base_url
            kwargs["default_headers"] = {
                "Authorization": f"Bearer {profile.api_key}",
            }
        client = anthropic.Anthropic(**kwargs)
        self._clients[profile.name] = client
        return client

    def list_profiles(self) -> list[dict[str, Any]]:
        now = time.time()
        rows: list[dict[str, Any]] = []
        for p in self.profiles:
            remaining = max(0.0, p.cooldown_until - now)
            rows.append({
                "name": p.name,
                "base_url": p.base_url or "(default)",
                "status": "available" if remaining <= 0 else f"cooldown({remaining:.0f}s)",
                "failure_reason": p.failure_reason,
                "last_good": (
                    time.strftime("%H:%M:%S", time.localtime(p.last_good_at))
                    if p.last_good_at > 0 else "never"
                ),
            })
        return rows


# ---------------------------------------------------------------------------
# 3. SimulatedFailure -- inject failures for testing
# ---------------------------------------------------------------------------


class SimulatedFailure:
    TEMPLATES: dict[str, str] = {
        "rate_limit": "Error code: 429 -- rate limit exceeded",
        "auth": "Error code: 401 -- invalid api key",
        "timeout": "Request timed out after 30s",
        "billing": "Error code: 402 -- billing quota exceeded",
        "overflow": "prompt is too long: context window token overflow",
        "unknown": "unexpected internal server error",
    }

    def __init__(self) -> None:
        self._pending: str | None = None

    def arm(self, reason: str) -> str:
        if reason not in self.TEMPLATES:
            return (
                f"Unknown reason '{reason}'. "
                f"Valid: {', '.join(self.TEMPLATES.keys())}"
            )
        self._pending = reason
        return f"Armed: next API call will fail with '{reason}'"

    def disarm(self) -> None:
        self._pending = None

    def check_and_fire(self) -> None:
        if self._pending is not None:
            reason = self._pending
            self._pending = None
            raise RuntimeError(self.TEMPLATES[reason])

    @property
    def is_armed(self) -> bool:
        return self._pending is not None

    @property
    def pending_reason(self) -> str | None:
        return self._pending


# ---------------------------------------------------------------------------
# 4. ResilienceRunner
# ---------------------------------------------------------------------------


class ResilienceExhausted(RuntimeError):
    """Raised when all profiles and fallback models have failed."""


CompactFn = Callable[[anthropic.Anthropic, list[dict]], None]


class ResilienceRunner:
    """Wraps one messages.create call with 3-layer retry logic.

    Layer 1 rotates AuthProfiles on auth/rate/timeout/billing failures.
    Layer 2 compacts messages in-place on overflow, then retries.
    Layer 3 is the tool-use loop that lives in agent.py.
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        simulated_failure: SimulatedFailure | None = None,
        verbose: bool = True,
    ) -> None:
        self.profile_manager = profile_manager
        self.simulated_failure = simulated_failure
        self.verbose = verbose

        self.total_attempts = 0
        self.total_successes = 0
        self.total_failures = 0
        self.total_compactions = 0
        self.total_rotations = 0
        self.total_fallbacks = 0

    @property
    def active_profile(self) -> AuthProfile | None:
        return self.profile_manager.select_profile()

    @property
    def active_client(self) -> anthropic.Anthropic | None:
        p = self.active_profile
        return self.profile_manager.client_for(p) if p else None

    def _log(self, text: str) -> None:
        if self.verbose:
            print(f"  [resilience] {text}")

    def call(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict],
        model: str,
        max_tokens: int,
        compact_fn: CompactFn | None = None,
        fallback_models: list[str] | None = None,
    ) -> tuple[Any, anthropic.Anthropic]:
        """Execute a single messages.create with the 3-layer retry onion.

        Mutates `messages` in place on overflow compaction. Returns the
        Anthropic response plus the client that successfully served it
        (so the caller can reuse it for subsequent calls in the same turn).
        """
        last_exc: Exception | None = None
        profiles_tried: set[str] = set()

        for _rotation in range(len(self.profile_manager.profiles)):
            profile = self.profile_manager.select_profile()
            if profile is None:
                self._log("all profiles on cooldown")
                break
            if profile.name in profiles_tried:
                break
            profiles_tried.add(profile.name)

            if len(profiles_tried) > 1:
                self.total_rotations += 1
                self._log(f"rotating to profile '{profile.name}'")

            client = self.profile_manager.client_for(profile)

            for compact_attempt in range(MAX_OVERFLOW_COMPACTION):
                self.total_attempts += 1
                try:
                    if self.simulated_failure:
                        self.simulated_failure.check_and_fire()

                    response = client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        tools=tools,
                        messages=messages,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    last_exc = exc
                    reason = classify_failure(exc)
                    self.total_failures += 1
                    self._log(
                        f"profile '{profile.name}' failed: {reason.value} -- {exc}"
                    )

                    if reason == FailoverReason.overflow:
                        if compact_fn is None or compact_attempt >= MAX_OVERFLOW_COMPACTION - 1:
                            self.profile_manager.mark_failure(
                                profile, reason, _COOLDOWN_OVERFLOW_EXHAUSTED,
                            )
                            break
                        self.total_compactions += 1
                        self._log(
                            f"overflow (attempt {compact_attempt + 1}/"
                            f"{MAX_OVERFLOW_COMPACTION}), compacting messages..."
                        )
                        try:
                            compact_fn(client, messages)
                        except Exception as compact_exc:
                            self._log(f"compact_fn failed: {compact_exc}")
                            self.profile_manager.mark_failure(
                                profile, reason, _COOLDOWN_OVERFLOW_EXHAUSTED,
                            )
                            break
                        continue

                    cd = _COOLDOWN_BY_REASON.get(reason, _COOLDOWN_UNKNOWN)
                    self.profile_manager.mark_failure(profile, reason, cd)
                    break
                else:
                    self.profile_manager.mark_success(profile)
                    self.total_successes += 1
                    return response, client

        for fb_model in fallback_models or []:
            profile = self.profile_manager.select_profile()
            if profile is None:
                for p in self.profile_manager.profiles:
                    if p.failure_reason in (
                        FailoverReason.rate_limit.value,
                        FailoverReason.timeout.value,
                    ):
                        p.cooldown_until = 0.0
                profile = self.profile_manager.select_profile()
            if profile is None:
                continue

            self.total_fallbacks += 1
            self._log(f"fallback: model='{fb_model}' profile='{profile.name}'")
            client = self.profile_manager.client_for(profile)

            self.total_attempts += 1
            try:
                if self.simulated_failure:
                    self.simulated_failure.check_and_fire()
                response = client.messages.create(
                    model=fb_model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_exc = exc
                self.total_failures += 1
                _classify_and_mark(self.profile_manager, profile, exc)
                self._log(f"fallback '{fb_model}' failed: {exc}")
                continue
            else:
                self.profile_manager.mark_success(profile)
                self.total_successes += 1
                return response, client

        raise ResilienceExhausted(
            f"All {len(profiles_tried)} profile(s) and "
            f"{len(fallback_models or [])} fallback model(s) exhausted. "
            f"Last error: {last_exc}"
        ) from last_exc

    def get_stats(self) -> dict[str, Any]:
        return {
            "attempts": self.total_attempts,
            "successes": self.total_successes,
            "failures": self.total_failures,
            "compactions": self.total_compactions,
            "rotations": self.total_rotations,
            "fallbacks": self.total_fallbacks,
        }


# ---------------------------------------------------------------------------
# 5. load_profiles
# ---------------------------------------------------------------------------


def load_profiles(
    keys_file: Path,
    env_api_key: str = "",
    env_base_url: str = "",
) -> list[AuthProfile]:
    """Build the profile list: `.env` is always the baseline, `keys.json` is additive.

    Precedence:
        1. If `env_api_key` is non-empty, add profile `env` first (using
           `env_base_url` as its base_url).
        2. Each entry in `keys.json::profiles` with a non-empty `api_key` is
           appended afterwards. Entries with empty `api_key` are silently
           ignored, so the scaffolded template (which ships with blank keys)
           is a no-op until the user fills it in.
        3. Duplicates by `api_key` are de-duped against earlier entries; this
           smooths over users whose `keys.json` still contains the same key
           as `.env` (e.g. from a pre-existing auto-migration).

    `keys.json` format:
        {
          "profiles": [
            {"name": "backup", "api_key": "sk-ant-...", "base_url": ""}
          ]
        }

    Profiles that omit `base_url` inherit `env_base_url` (so a proxy set in
    `.env` covers all extra profiles by default). An individual profile can
    still opt out by specifying its own `base_url`.
    """
    profiles: list[AuthProfile] = []
    seen_keys: set[str] = set()

    env_key = (env_api_key or "").strip()
    env_url = (env_base_url or "").strip()
    if env_key:
        profiles.append(AuthProfile(name="env", api_key=env_key, base_url=env_url))
        seen_keys.add(env_key)

    if keys_file.is_file():
        try:
            data = json.loads(keys_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to parse %s: %s", keys_file, exc)
            data = {}
        entries = data.get("profiles") if isinstance(data, dict) else None
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                api_key = str(entry.get("api_key", "")).strip()
                if not api_key:
                    continue
                if api_key in seen_keys:
                    log.debug(
                        "Skipping duplicate api_key in %s (entry %d)", keys_file, i,
                    )
                    continue
                name = str(entry.get("name", f"profile-{i + 1}")).strip() or f"profile-{i + 1}"
                base_url = str(entry.get("base_url", "")).strip() or env_url
                profiles.append(AuthProfile(
                    name=name, api_key=api_key, base_url=base_url,
                ))
                seen_keys.add(api_key)

    return profiles
