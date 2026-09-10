"""Application version metadata.

Kept in a dedicated module so that the version can be imported by the API,
bot and worker without any of them importing each other.
"""

from __future__ import annotations

import os
from typing import Final

APP_VERSION: Final[str] = "0.1.0"
"""Semantic version of the QUANTSPORT AI application."""

APP_PHASE: Final[str] = "phase-1-infrastructure"
"""Current development phase. Surfaced by health endpoints for traceability."""


def build_sha() -> str:
    """Return the git SHA baked into the image, or ``unknown`` for local runs."""
    return os.getenv("BUILD_SHA", "unknown")
