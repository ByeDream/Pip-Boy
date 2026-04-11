from __future__ import annotations

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
    """

    def __init__(self) -> None:
        self.enabled: bool = settings.profiler_enabled
        self._samples: list[Sample] = []
        self._stack: list[tuple[str, float]] = []

    def start(self, label: str) -> None:
        if not self.enabled:
            return
        self._stack.append((label, time.perf_counter()))

    def stop(self, **metadata: object) -> float:
        if not self.enabled or not self._stack:
            return 0.0
        label, t0 = self._stack.pop()
        elapsed_ms = (time.perf_counter() - t0) * 1000
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
        self._samples.append(
            Sample(label=label, elapsed_ms=elapsed_ms, metadata=dict(metadata))
        )

    def flush(self) -> None:
        """Print all collected samples for the current turn, then reset."""
        if not self.enabled or not self._samples:
            return
        parts: list[str] = []
        for s in self._samples:
            entry = f"{s.label}={s.elapsed_ms:.0f}ms"
            if s.metadata:
                meta = " ".join(f"{k}={v}" for k, v in s.metadata.items())
                entry += f" ({meta})"
            parts.append(entry)
        print(f"  [profiler] {' | '.join(parts)}")
        self._samples.clear()
