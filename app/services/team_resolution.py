"""Team identity resolution.

Providers disagree about team names. If "Man Utd" from the odds feed and
"Manchester United" from the historical feed become two separate teams, Elo
ratings and form statistics fragment silently and every downstream number is
wrong — without any error being raised. This service is the single place where
an external name becomes a canonical team.

Resolution order, cheapest and most certain first::

    normalize
        │
        ├─ 1. exact alias match      (already learned; confidence 1.0)
        ├─ 2. external ID match      (provider's own stable ID; 1.0)
        ├─ 3. exact canonical match  (normalized name is identical; 0.99)
        └─ 4. fuzzy candidate search (scored; may be ambiguous)
                │
                ├─ score >= auto_confirm_threshold → mapped and learned
                ├─ score >= review_threshold       → review queue, NOT used
                └─ below                           → unresolved

A low-confidence match is never silently accepted. Anything below the
auto-confirm threshold is queued for a human and the caller is told the team is
unresolved, so the fixture is marked unmodellable rather than being scanned
against the wrong team's history.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.enums import ReviewStatus
from app.database.models import Team, TeamAlias
from app.services.team_names import normalize_team_name, tokenize_team_name

logger = get_logger(__name__)

DEFAULT_AUTO_CONFIRM_THRESHOLD: Final[float] = 0.92
DEFAULT_REVIEW_THRESHOLD: Final[float] = 0.72
"""Below this, a candidate is treated as no match at all.

Calibrated against real abbreviations: "Man United" scores 0.731 against
"Manchester United", so a threshold of 0.75 would have let a duplicate club be
created rather than queuing it for a human. "Manchester United" against
"Manchester City" scores 0.700 and stays below the line.
"""
DEFAULT_CANDIDATE_LIMIT: Final[int] = 500

# A fuzzy match is rejected outright when a rival candidate scores almost as
# well: "Manchester United" against both "Manchester City" and "Manchester
# United" should never auto-confirm on margin alone.
CONTAINMENT_CONFIDENCE: Final[float] = 0.90
"""Confidence assigned to an unambiguous containment match.

High, because a unique containment is strong evidence — but below an exact
match, since decoration could in principle hide a genuine difference.
"""

AMBIGUITY_MARGIN: Final[float] = 0.05


class ResolutionMethod(StrEnum):
    """How a team was identified."""

    EXACT_ALIAS = "exact_alias"
    EXTERNAL_ID = "external_id"
    EXACT_CANONICAL = "exact_canonical"
    FUZZY = "fuzzy"
    NONE = "none"


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of a resolution attempt.

    Attributes:
        team: The resolved team, or ``None`` when unresolved.
        confidence: Score in ``[0, 1]``.
        method: Which stage produced the result.
        review_status: Review state of any alias written.
        needs_review: True when a candidate was found but not trusted.
        candidate: Best candidate when ``team`` is ``None``, for the reviewer.
        ambiguous: True when a rival candidate scored within the margin.
    """

    team: Team | None
    confidence: float
    method: ResolutionMethod
    review_status: ReviewStatus | None = None
    needs_review: bool = False
    candidate: Team | None = None
    ambiguous: bool = False

    @property
    def is_resolved(self) -> bool:
        """True only when the team may be used for modelling."""
        return self.team is not None


def contains_all_tokens(shorter: str, longer: str) -> bool:
    """Whether every token of one name appears in the other.

    Live feeds carry decorations the archive omits: founding years ("Darmstadt
    98", "Hannover 96"), club-type suffixes ("Mjallby AIF", "Clermont Foot"),
    sponsor prefixes ("Red Bull Salzburg") and place qualifiers ("Rakow
    Czestochowa"). Token similarity punishes each extra token hard — one extra
    out of two scores 0.50 — so these clubs looked entirely unknown despite
    being in the data under their short names.

    Containment recognises them. It is deliberately *not* a similarity score,
    because on its own it is dangerous: "Manchester" is contained in both
    United and City. The caller must confirm exactly one candidate contains the
    name before trusting it.
    """
    short_tokens = set(tokenize_team_name(shorter))
    long_tokens = set(tokenize_team_name(longer))
    if not short_tokens or not long_tokens:
        return False
    if short_tokens == long_tokens:
        return False
    return short_tokens < long_tokens


def similarity(left: str, right: str) -> float:
    """Score two normalized names between 0 and 1.

    Combines sequence similarity with token overlap. Sequence ratio alone
    rewards shared prefixes too heavily ("Manchester United" versus "Manchester
    City" scores highly); token overlap alone ignores ordering and spelling.
    The lower of the two is used, so both must agree before a name is trusted.

    Args:
        left: A normalized team name.
        right: A normalized team name.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    sequence_score = SequenceMatcher(None, left, right).ratio()

    left_tokens = tokenize_team_name(left)
    right_tokens = tokenize_team_name(right)
    if not left_tokens or not right_tokens:
        return 0.0

    # Each token scores against its best counterpart rather than requiring an
    # exact set intersection. Exact overlap is binary, so a single-token name
    # with one typo — "Arsenall" against "Arsenal" — scored zero and looked
    # like an entirely new club, which is precisely the silent identity split
    # this service exists to prevent.
    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    best = [
        max(SequenceMatcher(None, token, other).ratio() for other in longer) for token in shorter
    ]
    token_score = sum(best) / len(longer)

    # Both measures must agree. Sequence ratio alone over-rewards shared
    # prefixes ("Manchester United" against "Manchester City"); token matching
    # alone ignores word order and length.
    return min(sequence_score, token_score)


class TeamResolver:
    """Resolves provider team names to canonical teams."""

    def __init__(
        self,
        session: AsyncSession,
        auto_confirm_threshold: float = DEFAULT_AUTO_CONFIRM_THRESHOLD,
        review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> None:
        if not 0 < review_threshold <= auto_confirm_threshold <= 1:
            raise ValueError("Require 0 < review_threshold <= auto_confirm_threshold <= 1.")
        self._session = session
        self._auto_confirm = auto_confirm_threshold
        self._review = review_threshold
        self._candidate_limit = candidate_limit

    async def resolve(
        self,
        provider_name: str,
        raw_name: str,
        sport: str = "football",
        country: str | None = None,
        external_team_id: str | None = None,
        learn: bool = True,
    ) -> ResolutionResult:
        """Resolve one provider team name to a canonical team.

        Args:
            provider_name: Which provider supplied the name.
            raw_name: The name as the provider wrote it.
            sport: Sport, used to narrow candidates.
            country: Country, used to narrow candidates when known.
            external_team_id: The provider's own stable identifier, if any.
            learn: Whether to persist a new alias on a confident match.

        Returns:
            A :class:`ResolutionResult`. Callers must check ``is_resolved``
            before using the team; an unresolved fixture is unmodellable.
        """
        normalized = normalize_team_name(raw_name)
        if not normalized:
            return ResolutionResult(team=None, confidence=0.0, method=ResolutionMethod.NONE)

        alias_hit = await self._match_alias(provider_name, normalized)
        if alias_hit is not None:
            return alias_hit

        if external_team_id:
            external_hit = await self._match_external_id(provider_name, external_team_id)
            if external_hit is not None:
                return external_hit

        canonical_hit = await self._match_canonical(normalized, sport, country)
        if canonical_hit is not None and canonical_hit.team is not None:
            if learn:
                await self._write_alias(
                    canonical_hit.team,
                    provider_name,
                    raw_name,
                    normalized,
                    external_team_id,
                    confidence=canonical_hit.confidence,
                    review_status=ReviewStatus.AUTO_CONFIRMED,
                )
            return canonical_hit

        return await self._match_fuzzy(
            provider_name,
            raw_name,
            normalized,
            sport,
            country,
            external_team_id,
            learn,
        )

    async def _match_alias(self, provider_name: str, normalized: str) -> ResolutionResult | None:
        """Return a previously learned mapping, if one exists and is usable."""
        result = await self._session.execute(
            select(TeamAlias).where(
                TeamAlias.provider_name == provider_name,
                TeamAlias.normalized_alias == normalized,
            )
        )
        alias = result.scalar_one_or_none()
        if alias is None:
            return None

        if alias.review_status is ReviewStatus.REJECTED:
            return ResolutionResult(
                team=None,
                confidence=0.0,
                method=ResolutionMethod.EXACT_ALIAS,
                review_status=alias.review_status,
            )

        if alias.review_status is ReviewStatus.PENDING_REVIEW:
            candidate = await self._session.get(Team, alias.team_id)
            return ResolutionResult(
                team=None,
                confidence=float(alias.match_confidence or 0),
                method=ResolutionMethod.EXACT_ALIAS,
                review_status=alias.review_status,
                needs_review=True,
                candidate=candidate,
            )

        team = await self._session.get(Team, alias.team_id)
        return ResolutionResult(
            team=team,
            confidence=1.0,
            method=ResolutionMethod.EXACT_ALIAS,
            review_status=alias.review_status,
        )

    async def _match_external_id(
        self, provider_name: str, external_team_id: str
    ) -> ResolutionResult | None:
        """Match on the provider's own identifier, which is stable across renames."""
        result = await self._session.execute(
            select(TeamAlias).where(
                TeamAlias.provider_name == provider_name,
                TeamAlias.external_team_id == external_team_id,
                TeamAlias.is_confirmed.is_(True),
            )
        )
        alias = result.scalars().first()
        if alias is None:
            return None
        team = await self._session.get(Team, alias.team_id)
        return ResolutionResult(
            team=team,
            confidence=1.0,
            method=ResolutionMethod.EXTERNAL_ID,
            review_status=alias.review_status,
        )

    async def _match_canonical(
        self, normalized: str, sport: str, country: str | None
    ) -> ResolutionResult | None:
        """Match an identical normalized canonical name."""
        statement = select(Team).where(Team.sport == sport, Team.normalized_name == normalized)
        if country:
            statement = statement.where(Team.country == country)

        result = await self._session.execute(statement)
        teams = result.scalars().all()
        if len(teams) != 1:
            # Zero means no match. More than one means the same normalized name
            # exists in two countries and the caller did not disambiguate;
            # guessing here is exactly the silent error this service prevents.
            return None
        return ResolutionResult(
            team=teams[0], confidence=0.99, method=ResolutionMethod.EXACT_CANONICAL
        )

    async def _match_fuzzy(
        self,
        provider_name: str,
        raw_name: str,
        normalized: str,
        sport: str,
        country: str | None,
        external_team_id: str | None,
        learn: bool,
    ) -> ResolutionResult:
        """Score candidates and either accept, queue for review, or give up."""
        base = select(Team).where(Team.sport == sport)
        candidates: list[Team] = []
        if country:
            candidates = list(
                (
                    await self._session.execute(
                        base.where(Team.country == country).limit(self._candidate_limit)
                    )
                )
                .scalars()
                .all()
            )
        if not candidates:
            candidates = list(
                (await self._session.execute(base.limit(self._candidate_limit))).scalars().all()
            )
            if country:
                # The unscoped retry exists for teams whose country was never
                # recorded, not to match across borders. Two clubs in different
                # countries are never the same club, however alike the names:
                # "Celtic" against "Celta" and "St Mirren" against "St Etienne"
                # both scored above the review threshold and would otherwise sit
                # in the queue forever, withholding their fixtures.
                candidates = [
                    team for team in candidates if team.country is None or team.country == country
                ]
        if not candidates:
            return ResolutionResult(team=None, confidence=0.0, method=ResolutionMethod.NONE)

        # A name that fully contains a known club's name, plus decoration the
        # archive omits, is that club — but only when exactly one candidate
        # qualifies. "Manchester" is contained in both United and City, so an
        # ambiguous containment must be discarded rather than guessed.
        contained = [
            team
            for team in candidates
            if contains_all_tokens(team.normalized_name, normalized)
            or contains_all_tokens(normalized, team.normalized_name)
        ]
        if len(contained) == 1:
            return await self._accept(
                contained[0],
                provider_name,
                raw_name,
                normalized,
                external_team_id,
                confidence=CONTAINMENT_CONFIDENCE,
                learn=learn,
            )

        scored = sorted(
            ((similarity(normalized, team.normalized_name), team) for team in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best_team = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        ambiguous = (best_score - runner_up) < AMBIGUITY_MARGIN

        if best_score >= self._auto_confirm and not ambiguous:
            if learn:
                await self._write_alias(
                    best_team,
                    provider_name,
                    raw_name,
                    normalized,
                    external_team_id,
                    confidence=best_score,
                    review_status=ReviewStatus.AUTO_CONFIRMED,
                )
            logger.info(
                "team.resolved_fuzzy",
                provider=provider_name,
                raw=raw_name,
                team_id=best_team.id,
                confidence=round(best_score, 4),
            )
            return ResolutionResult(
                team=best_team,
                confidence=best_score,
                method=ResolutionMethod.FUZZY,
                review_status=ReviewStatus.AUTO_CONFIRMED,
                ambiguous=False,
            )

        if best_score >= self._review:
            if learn:
                await self._write_alias(
                    best_team,
                    provider_name,
                    raw_name,
                    normalized,
                    external_team_id,
                    confidence=best_score,
                    review_status=ReviewStatus.PENDING_REVIEW,
                    note=(
                        "Ambiguous: runner-up scored " f"{runner_up:.4f} against {best_score:.4f}."
                        if ambiguous
                        else "Below auto-confirm threshold."
                    ),
                )
            logger.warning(
                "team.queued_for_review",
                provider=provider_name,
                raw=raw_name,
                candidate_id=best_team.id,
                confidence=round(best_score, 4),
                ambiguous=ambiguous,
            )
            return ResolutionResult(
                team=None,
                confidence=best_score,
                method=ResolutionMethod.FUZZY,
                review_status=ReviewStatus.PENDING_REVIEW,
                needs_review=True,
                candidate=best_team,
                ambiguous=ambiguous,
            )

        logger.info(
            "team.unresolved",
            provider=provider_name,
            raw=raw_name,
            best_score=round(best_score, 4),
        )
        return ResolutionResult(
            team=None,
            confidence=best_score,
            method=ResolutionMethod.NONE,
            candidate=best_team,
            ambiguous=ambiguous,
        )

    async def _provider_already_named(
        self, provider_name: str, team_id: int, normalized: str
    ) -> bool:
        """Whether this provider already knows the team by a different name.

        Used to reject fuzzy matches within a single source, where consistent
        spelling means a difference is evidence of a different club rather than
        a variant of the same one.
        """
        result = await self._session.execute(
            select(TeamAlias).where(
                TeamAlias.provider_name == provider_name,
                TeamAlias.team_id == team_id,
                TeamAlias.is_confirmed.is_(True),
                TeamAlias.normalized_alias != normalized,
            )
        )
        return result.first() is not None

    async def _accept(
        self,
        team: Team,
        provider_name: str,
        raw_name: str,
        normalized: str,
        external_team_id: str | None,
        confidence: float,
        learn: bool,
    ) -> ResolutionResult:
        """Accept a match, recording the alias when learning is enabled."""
        if learn:
            await self._write_alias(
                team,
                provider_name,
                raw_name,
                normalized,
                external_team_id,
                confidence=confidence,
                review_status=ReviewStatus.AUTO_CONFIRMED,
            )
        return ResolutionResult(
            team=team,
            confidence=confidence,
            method=ResolutionMethod.FUZZY,
        )

    async def _write_alias(
        self,
        team: Team,
        provider_name: str,
        raw_name: str,
        normalized: str,
        external_team_id: str | None,
        confidence: float,
        review_status: ReviewStatus,
        note: str | None = None,
    ) -> TeamAlias:
        """Persist a learned mapping so the next lookup is exact."""
        alias = TeamAlias(
            team_id=team.id,
            provider_name=provider_name,
            alias=raw_name,
            normalized_alias=normalized,
            external_team_id=external_team_id,
            match_confidence=Decimal(str(round(confidence, 4))),
            review_status=review_status,
            is_confirmed=review_status
            in (ReviewStatus.AUTO_CONFIRMED, ReviewStatus.MANUALLY_CONFIRMED),
            review_note=note,
        )
        self._session.add(alias)
        await self._session.flush()
        return alias

    async def pending_review(self, limit: int = 100) -> list[TeamAlias]:
        """Return aliases awaiting a human decision, oldest first."""
        result = await self._session.execute(
            select(TeamAlias)
            .where(TeamAlias.review_status == ReviewStatus.PENDING_REVIEW)
            .order_by(TeamAlias.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def confirm(self, alias_id: int, team_id: int | None = None) -> TeamAlias:
        """Approve a queued alias, optionally reassigning it to another team.

        Args:
            alias_id: The alias under review.
            team_id: Correct team, when the suggested candidate was wrong.

        Raises:
            LookupError: If the alias does not exist.
        """
        alias = await self._session.get(TeamAlias, alias_id)
        if alias is None:
            raise LookupError(f"No alias with id {alias_id}.")
        if team_id is not None:
            alias.team_id = team_id
        alias.review_status = ReviewStatus.MANUALLY_CONFIRMED
        alias.is_confirmed = True
        alias.match_confidence = Decimal("1.0")
        await self._session.flush()
        logger.info("team.alias_confirmed", alias_id=alias_id, team_id=alias.team_id)
        return alias

    async def reject(self, alias_id: int, note: str | None = None) -> TeamAlias:
        """Reject a queued alias so it is never used or re-suggested.

        Raises:
            LookupError: If the alias does not exist.
        """
        alias = await self._session.get(TeamAlias, alias_id)
        if alias is None:
            raise LookupError(f"No alias with id {alias_id}.")
        alias.review_status = ReviewStatus.REJECTED
        alias.is_confirmed = False
        alias.review_note = note
        await self._session.flush()
        logger.info("team.alias_rejected", alias_id=alias_id)
        return alias

    async def resolve_or_create(
        self,
        provider_name: str,
        raw_name: str,
        sport: str = "football",
        country: str | None = None,
        external_team_id: str | None = None,
        competition_id: int | None = None,
    ) -> tuple[ResolutionResult, bool]:
        """Resolve a team, creating a canonical record when there is no match.

        This is the bootstrap path used by the first ingestion of a
        competition, when ``teams`` is empty and there is nothing to resolve
        against. A team is created only when the resolver found *nothing*
        plausible. An uncertain match still goes to the review queue and no new
        canonical record is created, because creating one would permanently
        split a club's identity in two — the exact failure the resolver exists
        to prevent.

        The canonical name is whatever the first source called the club. That
        is metadata, not identity: the internal id is the identity, so an
        administrator can correct ``canonical_name`` later without touching a
        single foreign key or historical record.

        Args:
            provider_name: Source supplying the name.
            raw_name: Name as written by the source.
            sport: Sport, used to scope candidates.
            country: Country, used to scope candidates.
            external_team_id: Source's own identifier, if any.
            competition_id: Competition to associate a newly created team with.

        Returns:
            ``(result, created)``.
        """
        # Resolved without learning: a fuzzy match may still be rejected below,
        # and an alias written before that decision would both be wrong and
        # collide with the alias for the team we go on to create.
        result = await self.resolve(
            provider_name,
            raw_name,
            sport=sport,
            country=country,
            external_team_id=external_team_id,
            learn=False,
        )

        # A single source spells each club exactly one way. So if a fuzzy match
        # lands on a team this same provider already named differently, the two
        # are different clubs, however similar the strings look.
        #
        # "Reggiana" and "Reggina" score 0.9333 and would otherwise merge into
        # one team, silently combining two clubs' entire histories. Both appear
        # in the same Serie B dataset, which is exactly the evidence that they
        # are distinct.
        # Applies to review-band matches too, not only confirmed ones. Burnley
        # against Barnsley scores 0.80 and lands in review, but both clubs are
        # already in the same dataset under their own names — so asking a human
        # is asking a question the data has already answered, while thousands
        # of matches sit withheld waiting for the reply.
        candidate = result.team or result.candidate
        if (
            result.method is ResolutionMethod.FUZZY
            and candidate is not None
            and await self._provider_already_named(
                provider_name, candidate.id, normalize_team_name(raw_name)
            )
        ):
            logger.info(
                "team.fuzzy_rejected_same_provider",
                provider=provider_name,
                raw=raw_name,
                candidate=candidate.canonical_name,
                confidence=round(result.confidence, 4),
            )
            result = ResolutionResult(
                team=None,
                confidence=result.confidence,
                method=ResolutionMethod.NONE,
            )

        if result.is_resolved and result.team is not None:
            if result.method is not ResolutionMethod.EXACT_ALIAS:
                await self._write_alias(
                    result.team,
                    provider_name,
                    raw_name,
                    normalize_team_name(raw_name),
                    external_team_id,
                    confidence=result.confidence,
                    review_status=ReviewStatus.AUTO_CONFIRMED,
                )
            return result, False

        # An uncertain match must never become a second canonical team: that
        # would split one club's history across two ids. But a candidate that
        # scored *below* the review threshold is not an uncertain match, it is
        # no match — the resolver returns the nearest name it saw regardless of
        # how poor the score was. Only ``needs_review`` means genuine doubt.
        if result.needs_review:
            # Queued here rather than by ``resolve``, which was asked not to
            # learn. Without this the doubt would be discarded and the same
            # name would be re-examined from scratch on every ingestion.
            #
            # Only a *fresh* fuzzy match is queued. When the doubt came from an
            # alias already in the queue, writing another would duplicate the
            # row — two spellings differing only in case normalize to one key,
            # so the second arrival finds the first and must leave it alone.
            if result.candidate is not None and result.method is ResolutionMethod.FUZZY:
                await self._write_alias(
                    result.candidate,
                    provider_name,
                    raw_name,
                    normalize_team_name(raw_name),
                    external_team_id,
                    confidence=result.confidence,
                    review_status=ReviewStatus.PENDING_REVIEW,
                    note="Below the auto-confirm threshold; needs a decision.",
                )
            return result, False

        normalized = normalize_team_name(raw_name)
        if not normalized:
            return result, False

        team = Team(
            canonical_name=raw_name.strip(),
            normalized_name=normalized,
            country=country,
            sport=sport,
            competition_id=competition_id,
        )
        self._session.add(team)
        await self._session.flush()

        await self._write_alias(
            team,
            provider_name,
            raw_name,
            normalized,
            external_team_id,
            confidence=1.0,
            review_status=ReviewStatus.AUTO_CONFIRMED,
            note="Canonical record bootstrapped from first ingestion.",
        )
        logger.info(
            "team.bootstrapped",
            provider=provider_name,
            raw=raw_name,
            team_id=team.id,
        )
        return (
            ResolutionResult(
                team=team,
                confidence=1.0,
                method=ResolutionMethod.EXACT_CANONICAL,
                review_status=ReviewStatus.AUTO_CONFIRMED,
            ),
            True,
        )
