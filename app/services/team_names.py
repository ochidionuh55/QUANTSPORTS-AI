"""Team name normalization.

Providers spell the same club differently: "Man Utd", "Manchester Utd",
"Manchester United FC". Normalization reduces these to a comparable form before
any matching is attempted, so that the fuzzy stage is only asked to handle
genuine ambiguity rather than punctuation and legal suffixes.

Normalization is deliberately conservative. It never drops a token that
distinguishes two real clubs. "United" is kept because Manchester United and
Manchester City differ by it; "FC" is dropped because no pair of clubs is
distinguished by it alone.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# Legal and structural suffixes that carry no distinguishing information.
# Ordered longest-first so that "FC" inside "AFC" is not stripped early.
_NOISE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "fc",
        "afc",
        "cf",
        "sc",
        "ac",
        "cd",
        "sv",
        "vfl",
        "vfb",
        "bsc",
        "fk",
        "nk",
        "hk",
        "if",
        "ik",
        "bk",
        "club",
        "futbol",
        "football",
        "calcio",
        "sport",
        "sports",
        "sportif",
        "sportive",
        "association",
        "de",
        "the",
    }
)

# Abbreviations expanded before comparison. Keys are matched as whole tokens.
_EXPANSIONS: Final[dict[str, str]] = {
    "utd": "united",
    "st": "saint",
    "ste": "saint",
    "athl": "athletic",
    "atl": "atletico",
    "real": "real",
    "spor": "spor",
    "wdrs": "wanderers",
    "wxs": "wanderers",
    "cty": "city",
    "twn": "town",
    "rvrs": "rovers",
    "acad": "academy",
    "univ": "university",
    "u": "under",
}

_ROMAN_NUMERAL_SUFFIX: Final[re.Pattern[str]] = re.compile(r"^(i{1,3}|iv|v)$")
_AGE_GROUP: Final[re.Pattern[str]] = re.compile(r"^u(\d{2})$")
_NON_ALPHANUMERIC: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9\s]")
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def strip_accents(value: str) -> str:
    """Remove diacritics so "Atlético" and "Atletico" compare equal."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def tokenize_team_name(raw: str) -> list[str]:
    """Reduce a raw team name to comparable tokens.

    Age-group markers such as "U21" are preserved as a distinct token, because
    a youth side is a different team from the senior side and must never be
    silently merged with it.

    Args:
        raw: The provider's team name.

    Returns:
        Normalized tokens, in order, with noise removed.
    """
    text = strip_accents(raw).lower()
    text = text.replace("&", " and ").replace("-", " ").replace(".", " ")
    text = _NON_ALPHANUMERIC.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    tokens: list[str] = []
    for token in text.split(" "):
        if not token:
            continue

        age_group = _AGE_GROUP.match(token)
        if age_group:
            tokens.append(f"u{age_group.group(1)}")
            continue

        expanded = _EXPANSIONS.get(token, token)
        if expanded in _NOISE_TOKENS:
            continue
        if _ROMAN_NUMERAL_SUFFIX.match(expanded) and tokens:
            # Trailing "II" marks a reserve side; keep it, it is meaningful.
            tokens.append(expanded)
            continue
        tokens.append(expanded)

    # Never return nothing: a name made entirely of noise tokens keeps its
    # original form rather than collapsing to an empty string that would match
    # every other empty result.
    if not tokens:
        return [text] if text else []
    return tokens


def normalize_team_name(raw: str) -> str:
    """Return the canonical comparable form of a team name.

    Args:
        raw: The provider's team name.

    Returns:
        Space-joined normalized tokens, or an empty string for empty input.
    """
    return " ".join(tokenize_team_name(raw))
