"""Dependency-free helpers for filesystem path trust boundaries."""

from __future__ import annotations

import os
from pathlib import Path


def lexical_absolute(path: os.PathLike[str] | str) -> Path:
    """Normalize ``.``/``..`` without following symbolic links."""

    return Path(os.path.abspath(os.fspath(path)))
