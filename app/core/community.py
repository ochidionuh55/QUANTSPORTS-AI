"""The QUANTSPORT community channel.

Defined in one place because it appears in the bot, on the website and in
metadata — and a channel link that is right in two of those and stale in the
third sends people to a dead page from whichever surface was forgotten.
"""

from __future__ import annotations

from typing import Final

CHANNEL_USERNAME: Final[str] = "PITCHIQ2"
CHANNEL_URL: Final[str] = f"https://t.me/{CHANNEL_USERNAME}"
CHANNEL_ID: Final[str] = f"@{CHANNEL_USERNAME}"
"""Form Telegram expects when asking whether someone is a member."""

CHANNEL_NAME: Final[str] = "PitchIQ"

JOIN_PROMPT: Final[str] = (
    "<b>📣 Join the QUANTSPORT channel</b>\n\n"
    "Daily selections, results as they settle, and what the models got wrong "
    "as well as right.\n\n"
    "It is where the record gets discussed openly — including the days that "
    "did not work."
)
