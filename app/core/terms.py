"""Terms of use and responsible-use notices.

The text is versioned. When a material change is made, bump ``TERMS_VERSION``
and every user whose recorded acceptance predates it is required to re-accept
before the bot will do anything else. Cosmetic edits — typos, formatting —
must not bump the version, or users are re-prompted for nothing.

Language here is deliberately factual. No profit claims, no guarantees, no
persuasion.
"""

from __future__ import annotations

from typing import Final

TERMS_VERSION: Final[str] = "1.0"
"""Current terms version. Bump ONLY for material changes."""

MINIMUM_AGE: Final[int] = 18

AGE_NOTICE: Final[str] = (
    f"<b>{MINIMUM_AGE}+ only</b>\n"
    f"This service is restricted to people aged {MINIMUM_AGE} or over, and "
    "only where sports betting is legal in your jurisdiction."
)

ESTIMATES_NOTICE: Final[str] = (
    "<b>Estimates, not predictions</b>\n"
    "QUANTSPORT AI produces statistical estimates of match outcomes. An "
    "estimate is a probability, not a forecast of what will happen. Every "
    "estimate carries uncertainty, and some will be wrong."
)

NO_GUARANTEE_NOTICE: Final[str] = (
    "<b>No guaranteed returns</b>\n"
    "Nothing here guarantees any return. This is not financial advice. Past "
    "performance does not indicate future results. Never stake money you "
    "cannot afford to lose."
)

RESPONSIBLE_USE_NOTICE: Final[str] = (
    "<b>Responsible use</b>\n"
    "Betting carries a real risk of financial loss and can be harmful. If you "
    "are worried about your gambling, seek support from a qualified service in "
    "your country."
)


def acceptance_prompt() -> str:
    """Return the full acceptance text shown before any functionality."""
    return (
        "<b>Before you continue</b>\n\n"
        f"{AGE_NOTICE}\n\n"
        f"{ESTIMATES_NOTICE}\n\n"
        f"{NO_GUARANTEE_NOTICE}\n\n"
        f"{RESPONSIBLE_USE_NOTICE}\n\n"
        "Confirm below to continue."
    )


def reacceptance_prompt(previous_version: str | None) -> str:
    """Return the text shown when terms have materially changed.

    Args:
        previous_version: The version the user previously accepted.
    """
    seen = previous_version or "an earlier version"
    return (
        "<b>Our terms have been updated</b>\n\n"
        f"You accepted {seen}. The current version is {TERMS_VERSION}.\n"
        "Please review and confirm again to continue.\n\n"
        f"{AGE_NOTICE}\n\n"
        f"{ESTIMATES_NOTICE}\n\n"
        f"{NO_GUARANTEE_NOTICE}\n\n"
        f"{RESPONSIBLE_USE_NOTICE}"
    )


CONFIRM_BUTTON_TEXT: Final[str] = f"I confirm I am {MINIMUM_AGE}+"
CONFIRM_CALLBACK_DATA: Final[str] = f"terms:accept:{TERMS_VERSION}"
DECLINE_BUTTON_TEXT: Final[str] = "No, exit"
DECLINE_CALLBACK_DATA: Final[str] = "terms:decline"

GATE_BLOCKED_MESSAGE: Final[str] = (
    "Please confirm the notice above before continuing. Send /start if you no " "longer have it."
)

DECLINED_MESSAGE: Final[str] = (
    "Understood. No functionality is available without confirmation.\n\n"
    "Send /start if you change your mind."
)
