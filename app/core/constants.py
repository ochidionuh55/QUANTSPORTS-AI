"""Shared constants.

The responsible-use text lives here so that every surface — Telegram, API
documentation, future web views — renders the same wording, and so that
changing it is a one-line change rather than a search-and-replace.
"""

from __future__ import annotations

from typing import Final

MINIMUM_AGE: Final[int] = 18

RESPONSIBLE_USE_NOTICE: Final[str] = (
    "QUANTSPORT AI produces statistical estimates of match outcomes. "
    "Estimates are not predictions, carry uncertainty, and offer no guarantee "
    "of any return. Nothing here is financial advice. "
    f"Use of this service is restricted to people aged {MINIMUM_AGE} or over, "
    "and only where sports betting is legal. "
    "Never stake money you cannot afford to lose."
)

NO_QUALIFIED_OPPORTUNITY: Final[str] = "NO QUALIFIED OPPORTUNITY FOUND."
"""Canonical response when no selection passes the filters. Never override."""

PHASE_ONE_BOT_NOTICE: Final[str] = (
    "QUANTSPORT AI is under construction.\n\n"
    "The platform is currently at the infrastructure stage and has no user "
    "features yet. No analysis, selections or booking codes are available."
)
