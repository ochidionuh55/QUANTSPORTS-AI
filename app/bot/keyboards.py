"""Keyboards.

The main menu is built from a registry of features, each declaring whether it
is available in the current build. Nothing unavailable is rendered, so the user
never taps a button that apologises for not existing.

Availability is not security. Every feature is also enforced server-side; the
menu only avoids advertising what cannot be used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.core.config import FeatureFlags
from app.core.terms import (
    CONFIRM_BUTTON_TEXT,
    CONFIRM_CALLBACK_DATA,
    DECLINE_BUTTON_TEXT,
    DECLINE_CALLBACK_DATA,
)


@dataclass(frozen=True)
class MenuFeature:
    """One entry in the main menu.

    Attributes:
        label: Button text.
        callback_data: Callback payload.
        phase: Development phase that delivers it, for documentation.
        available: Whether it works in this build.
    """

    label: str
    callback_data: str
    phase: int
    available: bool


def menu_features(features: FeatureFlags) -> list[MenuFeature]:
    """Return every known menu feature with its current availability.

    Phase 3 delivers account and help only. Scanning, predictions, booking
    codes and payments are declared here so the menu grows by flipping a flag
    rather than by editing layout code, but they render only once their phase
    has actually landed.

    Args:
        features: Runtime feature flags.
    """
    return [
        MenuFeature("🔥 Best of today", "menu:best", phase=13, available=True),
        MenuFeature("📊 Market explorer", "menu:markets", phase=13, available=True),
        MenuFeature("👥 Team intelligence", "menu:teams", phase=13, available=True),
        MenuFeature("📚 History", "menu:history", phase=13, available=True),
        MenuFeature("📈 Track record", "menu:record", phase=13, available=True),
        MenuFeature("⚽ Today's analysis", "menu:today", phase=12, available=True),
        MenuFeature("📊 Explore markets", "menu:explore", phase=12, available=True),
        MenuFeature("🏆 Competitions", "menu:competitions", phase=13, available=True),
        MenuFeature("🎯 Daily boards", "menu:highlights", phase=12, available=True),
        MenuFeature("📣 Community", "menu:community", phase=13, available=True),
        MenuFeature("📖 How it works", "menu:how_it_works", phase=3, available=True),
        MenuFeature("👤 My account", "menu:account", phase=3, available=True),
        MenuFeature("📜 Terms and safety", "menu:terms", phase=3, available=True),
        MenuFeature("Value selections", "menu:scan", phase=9, available=False),
        MenuFeature(
            "Generate booking code",
            "menu:booking",
            phase=11,
            available=features.booking_codes_enabled,
        ),
        MenuFeature(
            "Buy credits",
            "menu:credits",
            phase=12,
            available=features.payments_enabled,
        ),
    ]


def main_menu(features: FeatureFlags) -> InlineKeyboardMarkup:
    """Build the main menu from the features that are actually available."""
    buttons = [
        InlineKeyboardButton(text=feature.label, callback_data=feature.callback_data)
        for feature in menu_features(features)
        if feature.available
    ]

    # Two per row. A single column of fifteen buttons fills a phone screen
    # entirely, which makes the product feel like a list to scroll rather than
    # a place to choose from.
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def acceptance_keyboard() -> InlineKeyboardMarkup:
    """Build the age and terms confirmation keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONFIRM_BUTTON_TEXT, callback_data=CONFIRM_CALLBACK_DATA)],
            [InlineKeyboardButton(text=DECLINE_BUTTON_TEXT, callback_data=DECLINE_CALLBACK_DATA)],
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    """Build a single button returning to the main menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Back to menu", callback_data="menu:main")]]
    )


REMOVE_REPLY_KEYBOARD: Final[ReplyKeyboardMarkup | None] = None
"""No reply keyboard is used; the menu is inline so it can be edited in place."""


__all__ = [
    "KeyboardButton",
    "MenuFeature",
    "acceptance_keyboard",
    "back_to_menu",
    "main_menu",
    "menu_features",
]
