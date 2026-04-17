"""Crash-safe file I/O and platform-aware message chunking.

atomic_write: tmp + fsync + os.replace — no half-written files on crash.
chunk_message: split long text at paragraph boundaries per platform limits.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def atomic_write(path: Path, data: str) -> None:
    """Write *data* to *path* atomically via tmp → fsync → replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}.{path.name}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Platform-aware message chunking
# ---------------------------------------------------------------------------

CHANNEL_LIMITS: dict[str, int] = {
    "wechat": 2000,
    "wecom": 4096,
    "telegram": 4096,
    "discord": 2000,
    "default": 4096,
}


def chunk_message(text: str, channel: str = "default") -> list[str]:
    """Split *text* respecting *channel* size limits.

    Strategy: split on paragraph boundaries (``\\n\\n``) first, then fall
    back to line breaks, then spaces, then hard-cut.
    """
    if not text:
        return []
    limit = CHANNEL_LIMITS.get(channel, CHANNEL_LIMITS["default"])
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")

    return chunks or [text[:limit]]
