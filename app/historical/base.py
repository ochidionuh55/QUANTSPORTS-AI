"""Historical data provider contract.

Separate from ``OddsProvider`` by design. The two answer different questions,
have different failure modes and change on different schedules. A combined
interface would force an odds feed to pretend it knows about seasons and a
results archive to pretend it knows about live prices.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.historical.models import (
    HistoricalCompetition,
    HistoricalDatasetSnapshot,
    HistoricalSeason,
)
from app.providers.models import ProviderHealth, ProviderRole

logger = get_logger(__name__)


class HistoricalDataProvider(ABC):
    """A source of completed match results."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider identifier, written to ``provider_name`` columns."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable origin, recorded in dataset provenance."""

    @property
    @abstractmethod
    def parser_version(self) -> str:
        """Version of this provider's parsing logic.

        Bumped whenever parsing changes in a way that could alter output, so a
        snapshot can be tied to the code that produced it.
        """

    @property
    @abstractmethod
    def schema_version(self) -> str:
        """Version of the source schema this provider expects."""

    @abstractmethod
    async def get_competitions(self) -> tuple[HistoricalCompetition, ...]:
        """Return the competitions this source covers.

        Raises:
            HistoricalProviderError: On any source failure.
        """

    @abstractmethod
    async def get_seasons(self, competition_external_id: str) -> tuple[HistoricalSeason, ...]:
        """Return the seasons available for a competition.

        Raises:
            DatasetNotFoundError: If the competition is unknown.
        """

    @abstractmethod
    async def load_dataset(
        self, competition_external_id: str, season: str
    ) -> HistoricalDatasetSnapshot:
        """Load one competition-season, with provenance and rejects.

        Implementations must never silently discard a source record. Anything
        unusable is returned in ``snapshot.rejected`` with a reason.

        Raises:
            DatasetNotFoundError: If the competition or season is unavailable.
            DatasetParseError: If the dataset cannot be read at all.
        """

    async def health_check(self) -> ProviderHealth:
        """Probe the source. Must never raise.

        Reuses ``ProviderHealth`` so the API health report can treat odds and
        historical sources uniformly. ``ProviderRole.REFERENCE`` is used
        because historical data informs the prior; it is never tradeable.
        """
        return ProviderHealth(
            provider_name=self.name,
            provider_role=ProviderRole.REFERENCE,
            healthy=True,
            checked_at=datetime.now(UTC),
        )


class DatasetSource(ABC):
    """Where raw dataset bytes come from.

    Separated from parsing so the same parser can read a local file today, an
    object store tomorrow and a vendor export later, without the provider
    changing. This is the seam that keeps the CSV provider from being welded to
    one website.
    """

    @abstractmethod
    async def read(self, key: str) -> bytes:
        """Return raw bytes for a dataset key.

        Raises:
            DatasetNotFoundError: If the key does not exist.
        """

    @abstractmethod
    async def list_keys(self) -> tuple[str, ...]:
        """Return every available dataset key."""

    @property
    def origin(self) -> str | None:
        """Human-readable origin recorded in provenance, if meaningful."""
        return None
