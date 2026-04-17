from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from pip_agent.config import settings


@dataclass
class Sample:
    label: str
    elapsed_ms: float
    metadata: dict = field(default_factory=dict)


class Profiler:
    """Lightweight profiler that collects timing samples per turn.

    Disabled by default. Enable via PROFILER_ENABLED=true in .env.

    Thread-safety: the ``start``/``stop`` stack is per-thread (so concurrent
    lanes do not scramble each other's nested timings); the samples list and
    ``flush`` are guarded by a lock.
    """

    def __init__(self) -> None:
        self.enabled: bool = settings.profiler_enabled
        self._samples: list[Sample] = []
        self._tls = threading.local()
        self._lock = threading.Lock()

    def _stack(self) -> list[tuple[str, float]]:
        stk = getattr(self._tls, "stack", None)
        if stk is None:
            stk = []
            self._tls.stack = stk
        return stk

    def start(self, label: str) -> None:
        if not self.enabled:
            return
        self._stack().append((label, time.perf_counter()))

    def stop(self, **metadata: object) -> float:
        if not self.enabled:
            return 0.0
        stk = self._stack()
        if not stk:
            return 0.0
        label, t0 = stk.pop()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        with self._lock:
            self._samples.append(
                Sample(
                    label=label,
                    elapsed_ms=elapsed_ms,
                    metadata=dict(metadata),
                )
            )
        return elapsed_ms

    def record(self, label: str, elapsed_ms: float, **metadata: object) -> None:
        """Record a pre-computed sample (e.g. from a background thread)."""
        if not self.enabled:
            return
        with self._lock:
            self._samples.append(
                Sample(label=label, elapsed_ms=elapsed_ms, metadata=dict(metadata))
            )

    def flush(self) -> None:
        """Print all collected samples for the current turn, then reset."""
        if not self.enabled:
            return
        with self._lock:
            if not self._samples:
                return
            samples = list(self._samples)
            self._samples.clear()
        parts: list[str] = []
        for s in samples:
            entry = f"{s.label}={s.elapsed_ms:.0f}ms"
            if s.metadata:
                meta = " ".join(f"{k}={v}" for k, v in s.metadata.items())
                entry += f" ({meta})"
            parts.append(entry)
        print(f"  [profiler] {' | '.join(parts)}")
