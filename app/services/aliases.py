"""Team alias seeding.

The resolver refuses uncertain matches by design, which is right — a wrong
mapping silently corrupts a club's entire rating history. But that leaves a
long tail of legitimate spelling differences between our historical source and
the live fixture feed: ``Stoke`` against ``Stoke City`` scores 0.71, just under
the review threshold, so the fixture goes unanalysed.

Seeded aliases are the answer, and they are safe for a reason the fuzzy matcher
is not: **a human wrote each one down**. They are recorded as
``MANUALLY_CONFIRMED`` because that is what they are, not as a way of
sidestepping the threshold.

Seeding never creates a canonical team. If the target does not exist in our
history, the alias is skipped — an alias pointing at nothing would be a
fabrication.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.enums import ReviewStatus
from app.database.models import Team, TeamAlias
from app.services.team_names import normalize_team_name

logger = get_logger(__name__)

LIVE_PROVIDER: Final[str] = "api_football"

# Live feed spelling → the canonical name in our historical data.
#
# Only genuine identity matches belong here. Two different clubs that merely
# look similar must never appear, which is why the list is hand-written rather
# than generated from near-misses.
KNOWN_ALIASES: Final[dict[str, str]] = {
    # England — the feed prefers full legal names, the archive prefers short.
    "Stoke City": "Stoke",
    "Sheffield Utd": "Sheffield United",
    "Sheffield Wednesday": "Sheffield Weds",
    "Nottingham Forest": "Nott'm Forest",
    "West Bromwich Albion": "West Brom",
    "Queens Park Rangers": "QPR",
    "Wolverhampton Wanderers": "Wolves",
    "Manchester United": "Man United",
    "Manchester City": "Man City",
    "Newcastle United": "Newcastle",
    "Leeds United": "Leeds",
    "Leicester City": "Leicester",
    "Norwich City": "Norwich",
    "Swansea City": "Swansea",
    "Cardiff City": "Cardiff",
    "Hull City": "Hull",
    "Stoke-on-Trent": "Stoke",
    "Birmingham City": "Birmingham",
    "Coventry City": "Coventry",
    "Derby County": "Derby",
    "Ipswich Town": "Ipswich",
    "Luton Town": "Luton",
    "Preston North End": "Preston",
    "Bristol City": "Bristol City",
    "Blackburn Rovers": "Blackburn",
    "Huddersfield Town": "Huddersfield",
    "Middlesbrough FC": "Middlesbrough",
    "Rotherham United": "Rotherham",
    "Bolton Wanderers": "Bolton",
    "Charlton Athletic": "Charlton",
    "Millwall FC": "Millwall",
    "Watford FC": "Watford",
    "Brighton": "Brighton",
    "Tottenham": "Tottenham",
    "Nottingham": "Nott'm Forest",
    # Spain
    "Atletico Madrid": "Ath Madrid",
    "Athletic Club": "Ath Bilbao",
    "Real Betis": "Betis",
    "Real Sociedad": "Sociedad",
    "Celta Vigo": "Celta",
    "Deportivo Alaves": "Alaves",
    "Rayo Vallecano": "Vallecano",
    "Real Valladolid": "Valladolid",
    "Cadiz CF": "Cadiz",
    "Granada CF": "Granada",
    "Villarreal CF": "Villarreal",
    "Valencia CF": "Valencia",
    "Sevilla FC": "Sevilla",
    "FC Barcelona": "Barcelona",
    # Germany
    "Bayern Munich": "Bayern Munich",
    "Borussia Dortmund": "Dortmund",
    "Borussia Monchengladbach": "M'gladbach",
    "Bayer Leverkusen": "Leverkusen",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "1899 Hoffenheim": "Hoffenheim",
    "FC Koln": "FC Koln",
    "VfB Stuttgart": "Stuttgart",
    "Werder Bremen": "Werder Bremen",
    "VfL Wolfsburg": "Wolfsburg",
    "FC Augsburg": "Augsburg",
    "Mainz 05": "Mainz",
    "FSV Mainz 05": "Mainz",
    "Union Berlin": "Union Berlin",
    "SC Freiburg": "Freiburg",
    # Italy
    "AC Milan": "Milan",
    "Inter": "Inter",
    "AS Roma": "Roma",
    "SSC Napoli": "Napoli",
    "Juventus": "Juventus",
    "Hellas Verona": "Verona",
    # France
    "Paris Saint-Germain": "Paris SG",
    "Olympique Marseille": "Marseille",
    "Olympique Lyonnais": "Lyon",
    "Stade Rennais": "Rennes",
    "LOSC Lille": "Lille",
    "AS Monaco": "Monaco",
    "OGC Nice": "Nice",
    "RC Lens": "Lens",
}


class AliasSeeder:
    """Applies curated aliases to the canonical team table."""

    def __init__(self, session: AsyncSession, sport: str = "football") -> None:
        self._session = session
        self._sport = sport

    async def seed(
        self,
        aliases: dict[str, str] | None = None,
        provider_name: str = LIVE_PROVIDER,
    ) -> dict[str, int]:
        """Create confirmed aliases for known spelling differences.

        Args:
            aliases: Feed spelling to canonical name. Defaults to
                :data:`KNOWN_ALIASES`.
            provider_name: Which provider uses the feed spelling.

        Returns:
            Counts of ``created``, ``existing`` and ``skipped``.
        """
        mapping = aliases if aliases is not None else KNOWN_ALIASES
        counts = {"created": 0, "existing": 0, "skipped": 0}

        # Two different spellings can normalize to the same string —
        # "Paris Saint-Germain" and "Paris Saint Germain" both become
        # "paris saint germain". The database check below cannot catch that,
        # because neither row is flushed until the batch ends, so the pair is
        # tracked here as well.
        staged: set[str] = set()

        for feed_name, canonical_name in mapping.items():
            normalized_alias = normalize_team_name(feed_name)
            if not normalized_alias:
                counts["skipped"] += 1
                continue
            if normalized_alias in staged:
                counts["existing"] += 1
                continue

            team = await self._find_team(canonical_name)
            if team is None:
                # No canonical record: the club is outside our historical
                # coverage. An alias pointing at nothing would invent identity.
                counts["skipped"] += 1
                continue

            existing = await self._session.execute(
                select(TeamAlias).where(
                    TeamAlias.provider_name == provider_name,
                    TeamAlias.normalized_alias == normalized_alias,
                )
            )
            if existing.scalar_one_or_none() is not None:
                counts["existing"] += 1
                continue

            staged.add(normalized_alias)
            self._session.add(
                TeamAlias(
                    team_id=team.id,
                    provider_name=provider_name,
                    alias=feed_name,
                    normalized_alias=normalized_alias,
                    match_confidence=None,
                    review_status=ReviewStatus.MANUALLY_CONFIRMED,
                    is_confirmed=True,
                    review_note="Seeded from the curated alias list.",
                )
            )
            counts["created"] += 1

        await self._session.flush()
        logger.info("alias.seeded", **counts)
        return counts

    async def _find_team(self, canonical_name: str) -> Team | None:
        """Find a canonical team by exact normalized name."""
        normalized = normalize_team_name(canonical_name)
        result = await self._session.execute(
            select(Team).where(Team.sport == self._sport, Team.normalized_name == normalized)
        )
        return result.scalars().first()


async def prune_superseded_reviews(
    session: AsyncSession, provider_name: str = "csv_historical"
) -> list[str]:
    """Delete queued aliases that the same-provider rule now answers.

    A queued alias short-circuits resolution: the name matches it, the resolver
    reports doubt, and the match stays withheld. So entries queued under the
    old logic keep blocking fixtures even after the logic improves.

    An entry is superseded when the provider already names that team
    differently — "Burnley" queued against Barnsley, while Barnsley is itself
    confirmed under its own name from the same source. That is now decided
    automatically as two different clubs, so the question no longer needs
    asking.

    Entries with no such evidence are left alone: they represent genuine doubt
    and still need a human.

    Returns:
        The alias names removed.
    """
    pending = (
        (
            await session.execute(
                select(TeamAlias).where(
                    TeamAlias.provider_name == provider_name,
                    TeamAlias.review_status == ReviewStatus.PENDING_REVIEW,
                )
            )
        )
        .scalars()
        .all()
    )

    removed: list[str] = []
    for alias in pending:
        conflicting = await session.execute(
            select(TeamAlias.id).where(
                TeamAlias.provider_name == provider_name,
                TeamAlias.team_id == alias.team_id,
                TeamAlias.is_confirmed.is_(True),
                TeamAlias.normalized_alias != alias.normalized_alias,
            )
        )
        if conflicting.first() is not None:
            removed.append(alias.alias)
            await session.delete(alias)

    await session.flush()
    if removed:
        logger.info("alias.pruned_superseded_reviews", count=len(removed))
    return removed


class AliasReviewService:
    """Admin operations over the alias review queue."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def pending(self, limit: int = 20) -> list[TeamAlias]:
        """Return aliases awaiting a decision, oldest first."""
        result = await self._session.execute(
            select(TeamAlias)
            .where(TeamAlias.review_status == ReviewStatus.PENDING_REVIEW)
            .order_by(TeamAlias.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def search_teams(self, query: str, limit: int = 8) -> list[Team]:
        """Find canonical teams whose name contains ``query``."""
        normalized = normalize_team_name(query)
        if not normalized:
            return []
        result = await self._session.execute(
            select(Team).where(Team.normalized_name.contains(normalized.split(" ")[0])).limit(limit)
        )
        return list(result.scalars().all())

    async def link(
        self, feed_name: str, team_id: int, provider_name: str = LIVE_PROVIDER
    ) -> TeamAlias:
        """Create or confirm an alias linking a feed name to a canonical team.

        Raises:
            LookupError: If the target team does not exist.
        """
        team = await self._session.get(Team, team_id)
        if team is None:
            raise LookupError(f"No team with id {team_id}.")

        normalized = normalize_team_name(feed_name)
        result = await self._session.execute(
            select(TeamAlias).where(
                TeamAlias.provider_name == provider_name,
                TeamAlias.normalized_alias == normalized,
            )
        )
        alias = result.scalar_one_or_none()

        if alias is None:
            alias = TeamAlias(
                team_id=team.id,
                provider_name=provider_name,
                alias=feed_name,
                normalized_alias=normalized,
            )
            self._session.add(alias)

        alias.team_id = team.id
        alias.review_status = ReviewStatus.MANUALLY_CONFIRMED
        alias.is_confirmed = True
        alias.review_note = "Approved by an administrator."
        await self._session.flush()

        logger.info("alias.linked", alias=feed_name, team_id=team.id, team=team.canonical_name)
        return alias

    async def reject(self, alias_id: int, note: str | None = None) -> TeamAlias:
        """Reject a queued alias so it is never used again.

        Raises:
            LookupError: If the alias does not exist.
        """
        alias = await self._session.get(TeamAlias, alias_id)
        if alias is None:
            raise LookupError(f"No alias with id {alias_id}.")
        alias.review_status = ReviewStatus.REJECTED
        alias.is_confirmed = False
        alias.review_note = note or "Rejected by an administrator."
        await self._session.flush()
        return alias
