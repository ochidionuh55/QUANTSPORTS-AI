"""Deciding whether a competition is in scope for our model.

**Why this is a module and not a few string checks.** The same class of bug has
now appeared three times: "championship" matched a substring of "champions" and
filed a domestic American league as continental; "Toppserien" matched nothing
and put Norway's women's top division among expansion candidates; "Femenil"
matched neither ``feminin`` nor ``femenin`` because Spanish drops the second
``i``. Each was a one-line fix in one script, and the next script repeated it.

Matching is on whole words where the term is a word, and against a curated id
list where the name carries no clue at all. Anything unmatched is reported as
unknown rather than assumed to be a senior men's league.

**Out of scope is not a quality judgement.** A reserve league's squad turnover
and a women's league's scoring environment differ enough from the senior men's
game that our rate model would be estimating something other than what it
assumes. That is a structural mismatch. It says nothing about how good the
competition or its data is, and a separate model could serve them properly.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Final


class Scope(str, Enum):
    """What kind of competition this is, for modelling purposes."""

    SENIOR_MENS_LEAGUE = "senior men's domestic league"
    SENIOR_MENS_CUP = "senior men's domestic cup"
    WOMENS = "women's competition"
    YOUTH_RESERVE = "youth or reserve"
    INTERNATIONAL_CLUB = "international club competition"
    INTERNATIONAL_NATIONAL = "international national-team competition"
    UNKNOWN = "unknown"


WOMENS_TERMS: Final[tuple[str, ...]] = (
    # English, French, Spanish, Portuguese, Italian, German, Dutch, Nordic,
    # Slavic and Turkish forms. "femenil" is the one that slipped through:
    # Spanish drops the second "i" that "femenina" and "feminin" both carry.
    "women", "womens", "ladies",
    "feminin", "feminine", "feminins", "feminina", "femenina", "femenino",
    "femenil", "femenil",
    "femminile", "feminino",
    "frauen", "damen",
    "dames", "vrouwen",
    "kvinner", "kvinnor", "naiset", "damallsvenskan", "toppserien",
    "zenska", "zhenskaya", "kadinlar", "kobiet",
    "w", "wsl",
)
"""Terms whose presence marks a women's competition.

Bare ``w`` is included because several feeds label a competition "Liga W" or
"Serie A W". It is matched as a whole word only, so it cannot fire inside
another word.
"""

WOMENS_LEAGUE_IDS: Final[frozenset[int]] = frozenset(
    {
        725,  # Toppserien, Norway
        724,  # Damallsvenskan, Sweden
        696,  # Frauen-Bundesliga, Germany
        44,  # FA Women's Super League, England
        139,  # Primera Division Femenina, Spain
        64,  # Division 1 Feminine, France
        673,  # Liga MX Femenil, Mexico
        1040,  # Serie A Femminile, Italy
    }
)
"""Competitions whose names give no reliable clue.

Curated, and incomplete by nature — which is why an unmatched competition
falls to UNKNOWN rather than being assumed to be a men's league.
"""

YOUTH_TERMS: Final[tuple[str, ...]] = (
    "u15", "u16", "u17", "u18", "u19", "u20", "u21", "u22", "u23",
    "youth", "junior", "juniores", "juvenil", "primavera", "jugend",
    "reserve", "reserves", "academy", "development",
    "sub20", "sub21", "sub23",
)

RESERVE_PHRASES: Final[tuple[str, ...]] = (
    "premier league 2",
    "b team",
    "castilla",
    "ii",
)
"""Reserve competitions named without any of the usual words."""

INTERNATIONAL_CLUB_TERMS: Final[tuple[str, ...]] = (
    "uefa", "conmebol", "concacaf", "afc", "caf", "ofc",
    "libertadores", "sudamericana", "recopa",
)

INTERNATIONAL_CLUB_PHRASES: Final[tuple[str, ...]] = (
    "champions league", "europa league", "conference league",
    "club world cup", "super cup", "caribbean club",
    "central american cup", "intercontinental cup",
)

INTERNATIONAL_NATIONAL_PHRASES: Final[tuple[str, ...]] = (
    "world cup", "nations league", "copa america", "friendlies",
    "african cup", "asian cup", "gold cup", "asian games",
    "olympic", "qualification", "euro qualif",
)


def _words(text: str) -> set[str]:
    """Split into lowercase words.

    Whole words only. Substring matching is what made "championship" look like
    a continental competition.
    """
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def classify_scope(
    name: str,
    country: str | None = None,
    kind: str | None = None,
    league_id: int | None = None,
) -> Scope:
    """Return what kind of competition this is.

    Args:
        name: The competition's name as the provider gives it.
        country: The provider's country, or ``None``.
        kind: The provider's own "League" or "Cup", where known. This comes
            from the leagues catalogue and is not present on fixtures.
        league_id: The provider's league id, for competitions whose names
            carry no usable signal.
    """
    lowered = f"{name} {country or ''}".lower()
    words = _words(lowered)

    if league_id is not None and league_id in WOMENS_LEAGUE_IDS:
        return Scope.WOMENS
    if words & set(WOMENS_TERMS):
        return Scope.WOMENS
    if words & set(YOUTH_TERMS):
        return Scope.YOUTH_RESERVE
    if any(phrase in lowered for phrase in RESERVE_PHRASES if phrase != "ii"):
        return Scope.YOUTH_RESERVE
    if "ii" in words:
        return Scope.YOUTH_RESERVE

    if any(phrase in lowered for phrase in INTERNATIONAL_NATIONAL_PHRASES):
        return Scope.INTERNATIONAL_NATIONAL
    if words & set(INTERNATIONAL_CLUB_TERMS):
        return Scope.INTERNATIONAL_CLUB
    if any(phrase in lowered for phrase in INTERNATIONAL_CLUB_PHRASES):
        return Scope.INTERNATIONAL_CLUB
    if not country or country.lower() == "world":
        return Scope.INTERNATIONAL_NATIONAL

    normalised = (kind or "").strip().lower()
    if normalised == "cup":
        # A cup draws teams from several divisions, so its fixtures span
        # scoring environments the rate model cannot compare.
        return Scope.SENIOR_MENS_CUP
    if normalised == "league":
        return Scope.SENIOR_MENS_LEAGUE

    # No catalogue type. Name-based cup detection only, and everything else
    # stays unknown: a smaller candidate list beats a confidently wrong one.
    if words & {"cup", "kupa", "kupasi", "copa", "coupe", "pokal", "taca", "beker"}:
        return Scope.SENIOR_MENS_CUP
    return Scope.UNKNOWN


def is_in_scope(scope: Scope) -> bool:
    """Whether our current rate model can be applied to this kind."""
    return scope is Scope.SENIOR_MENS_LEAGUE


__all__ = [
    "INTERNATIONAL_CLUB_PHRASES",
    "INTERNATIONAL_CLUB_TERMS",
    "INTERNATIONAL_NATIONAL_PHRASES",
    "WOMENS_LEAGUE_IDS",
    "WOMENS_TERMS",
    "YOUTH_TERMS",
    "Scope",
    "classify_scope",
    "is_in_scope",
]
