"""Deciding whether a competition and service may publish.

**Scoped deliberately.** The gate governs competitions listed in
:data:`GATED_COMPETITIONS` only. The existing thirty-eight publish exactly as
they did before, because a gate covering everything would mean an empty or
unreadable capability table silently halting all publication — an outage worse
than the one it guards against. New coverage is gated; established coverage is
not put at the mercy of a new table.

**Fail closed inside that scope.** For a gated competition, publication
requires a capability row that exists, matches the publishing model version and
reads ACTIVE. A missing row, an unknown service, a version mismatch, a
withheld verdict, insufficient evidence or a failed lookup all block. There is
no path that treats absence as permission.

**It adds a condition, it never removes one.** A fixture must still clear the
service's existing production threshold. The gate can only prevent a
publication that would otherwise have happened; it cannot cause one.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models.capabilities import PUBLISHABLE_STATES, ServiceCapability

logger = get_logger(__name__)

GATED_COMPETITIONS: frozenset[str] = frozenset(
    {"LEU", "AZA", "PRV", "PRB", "ALF", "NPF", "BRS", "ECB"}
)
"""Competitions whose publication requires a validated capability.

Wave 1 only. Everything outside this set is unaffected, which keeps the
thirty-eight established competitions independent of this table's health.
"""


@dataclass(frozen=True)
class Decision:
    """Whether a publication may proceed, and why not when it may not."""

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED_UNGATED = Decision(True, "competition is not gated")


class CapabilityGate:
    """Checks publication permission for gated competitions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._cache: dict[tuple[str, str, str], ServiceCapability | None] = {}

    @staticmethod
    def is_gated(competition_code: str | None) -> bool:
        """Whether this competition's publications require a capability."""
        return bool(competition_code) and competition_code in GATED_COMPETITIONS

    async def may_publish(
        self,
        competition_code: str | None,
        service_key: str,
        model_version: str,
    ) -> Decision:
        """Return whether this competition and service may publish.

        Args:
            competition_code: Our code for the fixture's competition. ``None``
                means unresolved, which is not gated — an unresolved fixture
                cannot reach a Wave 1 competition in the first place.
            service_key: The service proposing to publish.
            model_version: The version producing the numbers now. It must match
                what the capability was validated against, or the evidence
                describes a different model.
        """
        if not self.is_gated(competition_code):
            return ALLOWED_UNGATED

        key = (str(competition_code), service_key, model_version)
        if key in self._cache:
            capability = self._cache[key]
        else:
            try:
                found = await self._session.execute(
                    select(ServiceCapability).where(
                        ServiceCapability.competition_code == competition_code,
                        ServiceCapability.service_key == service_key,
                        ServiceCapability.model_version == model_version,
                    )
                )
                capability = found.scalar_one_or_none()
            except SQLAlchemyError as error:
                # A lookup that fails is not permission. Blocking a gated
                # competition costs coverage we did not have last week; the
                # alternative publishes something nothing has validated.
                logger.warning(
                    "capability.lookup_failed",
                    competition=competition_code,
                    service=service_key,
                    error=str(error),
                )
                return Decision(False, "capability lookup failed")
            self._cache[key] = capability

        if capability is None:
            return Decision(False, "no capability for this competition and service")
        if capability.state not in PUBLISHABLE_STATES:
            return Decision(False, f"capability is {capability.state}")
        return Decision(True, "capability validated")


__all__ = ["ALLOWED_UNGATED", "GATED_COMPETITIONS", "CapabilityGate", "Decision"]
