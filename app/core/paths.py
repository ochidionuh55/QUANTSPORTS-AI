"""Filesystem locations.

Resolved rather than hardcoded, because the data directory sits in a different
place depending on how the code is running: bind-mounted at ``/data`` in
development, baked into the image alongside the application in production, and
at the repository root when someone runs a script directly.

Hardcoding any one of those made the ingest script fail in production with
"No such directory: /data" on an image that contained the data all along.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

DATA_DIR_ENV: Final[str] = "QUANTSPORT_DATA_DIR"

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Return the historical CSV directory.

    Checked in order of specificity: an explicit environment variable, the
    development bind mount, then the copy shipped inside the image. The first
    that exists wins.

    Returns:
        The directory, which may not exist if none of the candidates do — the
        caller reports that with the paths it tried.
    """
    override = os.getenv(DATA_DIR_ENV)
    if override:
        return Path(override)

    for candidate in (Path("/data"), PROJECT_ROOT / "data"):
        if candidate.is_dir():
            return candidate

    return PROJECT_ROOT / "data"


def candidate_data_dirs() -> tuple[Path, ...]:
    """Return every location searched, for error messages."""
    override = os.getenv(DATA_DIR_ENV)
    candidates = [Path(override)] if override else []
    candidates.extend([Path("/data"), PROJECT_ROOT / "data"])
    return tuple(candidates)
