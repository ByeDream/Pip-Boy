"""Built-in theme directory.

Each subdirectory is one theme: ``theme.toml`` + ``theme.tcss`` +
optional ``art.txt``. Phase A ships only the default ``wasteland``
theme; Phase B will add the discovery walker that pulls every
subdirectory plus ``<workspace>/.pip/themes/`` into a unified list.
"""

from __future__ import annotations

from pathlib import Path

BUILTIN_THEMES_DIR: Path = Path(__file__).resolve().parent

__all__ = ["BUILTIN_THEMES_DIR"]
