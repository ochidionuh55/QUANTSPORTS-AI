"""Basketball fixtures provider tests.

The provider has one job beyond fetching: keeping "we can see this game" apart
from "we can say something about this game". Collapsing those would put NBA
probabilities on leagues that score sixty points fewer per match.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.providers.basketball import (
    BasketballFixture,
    BasketballProvider,
    BasketballProviderError,
    summarise,
)


def _response(records: list[dict[str, object]], status: int = 200) -> httpx.AsyncClient:
    """Build a client returning a fixed payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"response": records})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _game(league_id: int, identifier: int = 1) -> dict[str, object]:
    """Build one provider record."""
    return {
        "id": identifier,
        "date": "2026-09-14T18:00:00+00:00",
        "league": {"id": league_id},
        "teams": {"home": {"name": "Alpha"}, "away": {"name": "Bravo"}},
        "status": {"short": "NS"},
    }


class TestConfiguration:
    """Behaviour without a subscription."""

    async def test_unconfigured_provider_reports_itself(self) -> None:
        assert BasketballProvider(api_key=None).configured is False

    async def test_unconfigured_provider_raises_rather_than_returning_empty(
        self,
    ) -> None:
        """An empty list would look like a quiet day and hide a fixable
        problem."""
        with pytest.raises(BasketballProviderError) as error:
            await BasketballProvider(api_key="").fixtures()
        assert "BASKETBALL_API_KEY" in str(error.value)

    async def test_blank_key_is_not_configured(self) -> None:
        assert BasketballProvider(api_key="   ").configured is False


class TestParsing:
    """Turning provider records into fixtures."""

    async def test_known_league_is_parsed(self) -> None:
        client = _response([_game(13)])
        fixtures = await BasketballProvider("key", client=client).fixtures()

        assert len(fixtures) == 1
        assert fixtures[0].competition_key == "WNBA"

    async def test_unknown_league_is_discarded(self) -> None:
        """An unrecognised competition has no parameters and no honest
        coverage grade, so it is dropped rather than guessed at."""
        client = _response([_game(99999)])
        fixtures = await BasketballProvider("key", client=client).fixtures()

        assert fixtures == []

    async def test_incomplete_record_is_discarded(self) -> None:
        client = _response([{"id": 1, "league": {"id": 13}}])
        fixtures = await BasketballProvider("key", client=client).fixtures()

        assert fixtures == []

    async def test_fixtures_are_ordered_by_tip_off(self) -> None:
        early = _game(13, 1) | {"date": "2026-09-14T16:00:00+00:00"}
        late = _game(13, 2) | {"date": "2026-09-14T22:00:00+00:00"}
        client = _response([late, early])

        fixtures = await BasketballProvider("key", client=client).fixtures()
        assert [f.provider_event_id for f in fixtures] == ["1", "2"]


class TestSeeingVersusModelling:
    """The distinction the whole provider exists to preserve."""

    def test_nba_fixture_is_modellable(self) -> None:
        fixture = BasketballFixture("1", "NBA", "NBA", "Lakers", "Celtics", datetime.now(UTC))
        assert fixture.modellable
        assert fixture.coverage == "fully_modelled"
        assert fixture.unavailable_reason == ""

    def test_unmeasured_league_is_visible_but_not_modellable(self) -> None:
        """A WNBA game totals around 160 and an NBA game around 225; borrowing
        the parameters would be confidently wrong."""
        fixture = BasketballFixture("1", "WNBA", "WNBA", "Aces", "Lynx", datetime.now(UTC))
        assert fixture.modellable is False
        assert fixture.coverage == "unsupported"
        assert "no measured parameters" in fixture.unavailable_reason

    def test_every_unmeasured_league_explains_itself(self) -> None:
        for key in ("WNBA", "NBL", "EUROLEAGUE", "FIBA_WC"):
            fixture = BasketballFixture("1", key, key, "Alpha", "Bravo", datetime.now(UTC))
            assert fixture.unavailable_reason


class TestErrors:
    """Feed failures are reported, not swallowed."""

    async def test_rejected_key_is_explained(self) -> None:
        client = _response([], status=401)
        with pytest.raises(BasketballProviderError) as error:
            await BasketballProvider("bad", client=client).fixtures()
        assert "subscription" in str(error.value).lower()

    async def test_rate_limit_is_explained(self) -> None:
        client = _response([], status=429)
        with pytest.raises(BasketballProviderError) as error:
            await BasketballProvider("key", client=client).fixtures()
        assert "limit" in str(error.value).lower()


class TestSummary:
    """Counting by competition."""

    def test_counts_per_competition(self) -> None:
        now = datetime.now(UTC)
        fixtures = [
            BasketballFixture("1", "WNBA", "WNBA", "A", "B", now),
            BasketballFixture("2", "WNBA", "WNBA", "C", "D", now),
            BasketballFixture("3", "NBA", "NBA", "E", "F", now),
        ]
        assert summarise(fixtures) == {"WNBA": 2, "NBA": 1}
