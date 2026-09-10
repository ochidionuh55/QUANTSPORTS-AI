"""Phase 4B tests: honest mock pricing and historical data providers.

The pricing tests exist because bad fixtures produce false confidence. If every
tradeable outcome carried positive expected value, a value scanner that
accepted everything would pass its tests, and the defect would only surface as
unexplained losses much later.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.historical import (
    CsvHistoricalDataProvider,
    DatasetNotFoundError,
    DatasetSchemaError,
    HistoricalDataProvider,
    InMemorySource,
    MatchResult,
    MockHistoricalDataProvider,
    RejectionReason,
    content_hash,
    decode_csv,
    parse_match_date,
)
from app.historical.mock import PROMOTED_TEAM
from app.providers.mock import (
    APPARENT_VALUE_EVENT_ID,
    GENUINE_VALUE_EVENT_ID,
    MARGINAL_VALUE_EVENT_ID,
    NO_VALUE_EVENT_ID,
    UNDERDOG_VALUE_EVENT_ID,
    MockOddsProvider,
    fair_probabilities,
)
from app.providers.models import ProviderRole
from app.providers.pricing import (
    OutcomePricing,
    expected_value,
    price_market,
    remove_margin_proportional,
)

EDGE_THRESHOLD = Decimal("0.03")


@pytest.fixture
def tradeable() -> MockOddsProvider:
    """Soft book."""
    return MockOddsProvider(name="soft", role=ProviderRole.TRADEABLE)


@pytest.fixture
def reference() -> MockOddsProvider:
    """Sharp book."""
    return MockOddsProvider(name="sharp", role=ProviderRole.REFERENCE)


async def _odds(provider: MockOddsProvider, event_id: str, market: str) -> dict[str, Decimal]:
    """Return quoted odds for one market."""
    event = await provider.get_event(event_id)
    for m in event.markets:
        if m.external_id.endswith(market):
            return {o.name: o.odds for o in m.outcomes}
    raise AssertionError(f"market {market} missing from {event_id}")


async def _all_ev(provider: MockOddsProvider) -> list[Decimal]:
    """Return expected value for every priced outcome."""
    values: list[Decimal] = []
    for event_id in (
        NO_VALUE_EVENT_ID,
        GENUINE_VALUE_EVENT_ID,
        APPARENT_VALUE_EVENT_ID,
        MARGINAL_VALUE_EVENT_ID,
        UNDERDOG_VALUE_EVENT_ID,
    ):
        for market in ("1x2", "ou25"):
            fair = fair_probabilities(event_id, market)
            quoted = await _odds(provider, event_id, market)
            values.extend(expected_value(fair[k], quoted[k]) for k in fair)
    return values


class TestPricingModel:
    """Odds must be derived from probabilities, not offset from each other."""

    def test_overround_is_respected(self) -> None:
        outcomes = (
            OutcomePricing("Home", Decimal("0.5")),
            OutcomePricing("Away", Decimal("0.5")),
        )
        quoted = price_market(outcomes, overround=Decimal("1.08"))
        implied = sum(Decimal("1") / o for o in quoted.values())
        assert abs(implied - Decimal("1.08")) < Decimal("0.01")

    def test_tilt_redistributes_without_changing_margin(self) -> None:
        """Tilt must move margin between outcomes, not add to it."""
        outcomes = (
            OutcomePricing("Home", Decimal("0.5"), tilt=Decimal("0.9")),
            OutcomePricing("Away", Decimal("0.5"), tilt=Decimal("1.1")),
        )
        quoted = price_market(outcomes, overround=Decimal("1.08"))
        implied = sum(Decimal("1") / o for o in quoted.values())
        assert abs(implied - Decimal("1.08")) < Decimal("0.01")
        assert quoted["Home"] > quoted["Away"]

    def test_invalid_distribution_rejected(self) -> None:
        with pytest.raises(ValueError, match="sum to 1.0"):
            price_market((OutcomePricing("Home", Decimal("0.4")),), overround=Decimal("1.05"))

    def test_empty_market_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty market"):
            price_market((), overround=Decimal("1.05"))


class TestSharpBookIsSharper:
    """The reference book must be the tighter market, as in reality."""

    async def test_reference_quotes_longer_on_untilted_market(
        self, tradeable: MockOddsProvider, reference: MockOddsProvider
    ) -> None:
        """Taking less margin means offering better prices."""
        sharp = await _odds(reference, NO_VALUE_EVENT_ID, "1x2")
        soft = await _odds(tradeable, NO_VALUE_EVENT_ID, "1x2")
        assert all(sharp[k] > soft[k] for k in sharp)

    async def test_reference_margin_removal_recovers_fair_probabilities(
        self, reference: MockOddsProvider
    ) -> None:
        """The prior must be honest, or nothing downstream can be trusted."""
        quoted = await _odds(reference, NO_VALUE_EVENT_ID, "1x2")
        recovered = remove_margin_proportional(quoted)
        fair = fair_probabilities(NO_VALUE_EVENT_ID, "1x2")
        for label, probability in fair.items():
            assert abs(recovered[label] - probability) < Decimal("0.005")

    async def test_prices_are_not_a_flat_offset(
        self, tradeable: MockOddsProvider, reference: MockOddsProvider
    ) -> None:
        """A constant difference would betray an offset rather than a model."""
        sharp = await _odds(reference, GENUINE_VALUE_EVENT_ID, "1x2")
        soft = await _odds(tradeable, GENUINE_VALUE_EVENT_ID, "1x2")
        differences = {sharp[k] - soft[k] for k in sharp}
        assert len(differences) > 1


class TestExpectedValueDistribution:
    """The scanner must be forced to discriminate."""

    async def test_not_every_outcome_is_positive(self, tradeable: MockOddsProvider) -> None:
        """The defect this phase exists to fix."""
        values = await _all_ev(tradeable)
        assert any(v <= 0 for v in values)
        assert sum(1 for v in values if v > 0) < len(values) / 2

    async def test_some_outcomes_are_genuinely_positive(self, tradeable: MockOddsProvider) -> None:
        values = await _all_ev(tradeable)
        assert any(v > EDGE_THRESHOLD for v in values)

    async def test_positive_value_is_scarce(self, tradeable: MockOddsProvider) -> None:
        """Realistic scarcity: opportunities are the exception."""
        values = await _all_ev(tradeable)
        positive = sum(1 for v in values if v > 0)
        assert 0 < positive <= len(values) // 5

    async def test_no_value_fixture_has_none(self, tradeable: MockOddsProvider) -> None:
        fair = fair_probabilities(NO_VALUE_EVENT_ID, "1x2")
        quoted = await _odds(tradeable, NO_VALUE_EVENT_ID, "1x2")
        assert all(expected_value(fair[k], quoted[k]) < 0 for k in fair)

    async def test_genuine_value_fixture_has_a_real_edge(self, tradeable: MockOddsProvider) -> None:
        fair = fair_probabilities(GENUINE_VALUE_EVENT_ID, "1x2")
        quoted = await _odds(tradeable, GENUINE_VALUE_EVENT_ID, "1x2")
        assert expected_value(fair["Away"], quoted["Away"]) > EDGE_THRESHOLD

    async def test_apparent_value_is_a_trap(
        self, tradeable: MockOddsProvider, reference: MockOddsProvider
    ) -> None:
        """Longer than the sharp book, yet still negative expected value.

        A scanner comparing raw prices between books will flag this. Only one
        that removes margin first will reject it.
        """
        sharp = await _odds(reference, APPARENT_VALUE_EVENT_ID, "1x2")
        soft = await _odds(tradeable, APPARENT_VALUE_EVENT_ID, "1x2")
        fair = fair_probabilities(APPARENT_VALUE_EVENT_ID, "1x2")

        assert soft["Home"] > sharp["Home"]
        assert expected_value(fair["Home"], soft["Home"]) < 0

    async def test_marginal_edge_falls_below_threshold(self, tradeable: MockOddsProvider) -> None:
        fair = fair_probabilities(MARGINAL_VALUE_EVENT_ID, "1x2")
        quoted = await _odds(tradeable, MARGINAL_VALUE_EVENT_ID, "1x2")
        edge = expected_value(fair["Draw"], quoted["Draw"])
        assert 0 < edge < EDGE_THRESHOLD

    async def test_underdog_market_shape(self, tradeable: MockOddsProvider) -> None:
        """Heavy favourite shortened, underdog lengthened into value."""
        fair = fair_probabilities(UNDERDOG_VALUE_EVENT_ID, "1x2")
        quoted = await _odds(tradeable, UNDERDOG_VALUE_EVENT_ID, "1x2")
        assert quoted["Home"] < Decimal("1.5")
        assert quoted["Away"] > Decimal("6")
        assert expected_value(fair["Home"], quoted["Home"]) < 0
        assert expected_value(fair["Away"], quoted["Away"]) > EDGE_THRESHOLD

    async def test_role_alone_does_not_determine_ev_sign(self, tradeable: MockOddsProvider) -> None:
        """Being the tradeable book must not imply value, nor imply its absence."""
        values = await _all_ev(tradeable)
        assert any(v > 0 for v in values)
        assert any(v < 0 for v in values)

    async def test_accepting_everything_would_lose_money(self, tradeable: MockOddsProvider) -> None:
        """A scanner with no filter must be unprofitable on this data."""
        assert sum(await _all_ev(tradeable)) < 0


class TestHistoricalContract:
    """Contract and determinism of the mock historical provider."""

    def test_implements_the_contract(self) -> None:
        assert isinstance(MockHistoricalDataProvider(), HistoricalDataProvider)

    def test_abstract_base_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            HistoricalDataProvider()  # type: ignore[abstract]

    async def test_competitions_and_seasons(self) -> None:
        provider = MockHistoricalDataProvider()
        competitions = await provider.get_competitions()
        assert {c.external_id for c in competitions} == {"E0", "SP1"}

        seasons = await provider.get_seasons("E0")
        assert {s.season for s in seasons} == {"2023/2024", "2024/2025"}

    async def test_unknown_competition_raises(self) -> None:
        with pytest.raises(DatasetNotFoundError):
            await MockHistoricalDataProvider().get_seasons("XX")

    async def test_unknown_season_raises(self) -> None:
        with pytest.raises(DatasetNotFoundError):
            await MockHistoricalDataProvider().load_dataset("E0", "1999/2000")

    async def test_output_is_deterministic(self) -> None:
        first = await MockHistoricalDataProvider().load_dataset("E0", "2023/2024")
        second = await MockHistoricalDataProvider().load_dataset("E0", "2023/2024")
        assert first.matches == second.matches
        assert first.metadata.content_hash == second.metadata.content_hash

    async def test_results_cover_all_three_outcomes(self) -> None:
        snapshot = await MockHistoricalDataProvider().load_dataset("E0", "2023/2024")
        assert {m.result for m in snapshot.matches} == {
            MatchResult.HOME,
            MatchResult.DRAW,
            MatchResult.AWAY,
        }

    async def test_promoted_side_has_sparse_history(self) -> None:
        """Models must meet a team with almost no history."""
        snapshot = await MockHistoricalDataProvider().load_dataset("E0", "2024/2025")
        appearances = [
            m
            for m in snapshot.matches
            if PROMOTED_TEAM in (m.home_team.source_name, m.away_team.source_name)
        ]
        assert 0 < len(appearances) < 6

    async def test_rejected_records_are_reported_not_dropped(self) -> None:
        snapshot = await MockHistoricalDataProvider().load_dataset("E0", "2023/2024")
        assert snapshot.rejected
        assert snapshot.metadata.rejected_count == len(snapshot.rejected)
        assert snapshot.metadata.row_count == len(snapshot.matches) + len(snapshot.rejected)
        assert 0 < snapshot.acceptance_rate < 1

    async def test_teams_are_left_unresolved(self) -> None:
        """Canonical identity is assigned downstream, not in the provider."""
        snapshot = await MockHistoricalDataProvider().load_dataset("E0", "2023/2024")
        match = snapshot.matches[0]
        assert match.home_team.source_name
        assert not hasattr(match.home_team, "team_id")

    async def test_health_check_does_not_raise(self) -> None:
        health = await MockHistoricalDataProvider().health_check()
        assert health.healthy is True


HEADER = "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HTHG,HTAG,FTR\n"


def _csv(*rows: str) -> bytes:
    """Build a CSV payload."""
    return (HEADER + "".join(row + "\n" for row in rows)).encode()


def _provider(payload: bytes, key: str = "E0_2023-2024.csv") -> CsvHistoricalDataProvider:
    """Build a CSV provider over an in-memory dataset."""
    return CsvHistoricalDataProvider(
        source=InMemorySource({key: payload}),
        competitions={"E0": ("Premier League", "England")},
        source_url="https://example.invalid/E0.csv",
    )


class TestCsvParsing:
    """CSV parsing, validation and provenance."""

    async def test_parses_valid_rows(self) -> None:
        payload = _csv(
            "E0,12/08/2023,Arsenal,Chelsea,2,1,1,0,H",
            "E0,13/08/2023,Everton,Liverpool,0,3,0,1,A",
        )
        snapshot = await _provider(payload).load_dataset("E0", "2023/2024")

        assert len(snapshot.matches) == 2
        first = snapshot.matches[0]
        assert first.match_date == date(2023, 8, 12)
        assert first.result is MatchResult.HOME
        assert first.total_goals == 3
        assert first.both_teams_scored is True
        assert first.half_time_home_goals == 1

    async def test_day_first_dates(self) -> None:
        """Month-first parsing would silently shift a third of every season."""
        assert parse_match_date("01/02/2024") == date(2024, 2, 1)
        assert parse_match_date("01/02/24") == date(2024, 2, 1)
        assert parse_match_date("2024-02-01") == date(2024, 2, 1)

    async def test_missing_required_columns_raises(self) -> None:
        payload = b"Div,Date,HomeTeam\nE0,12/08/2023,Arsenal\n"
        with pytest.raises(DatasetSchemaError, match="missing required columns"):
            await _provider(payload).load_dataset("E0", "2023/2024")

    async def test_missing_dataset_raises(self) -> None:
        with pytest.raises(DatasetNotFoundError):
            await _provider(_csv()).load_dataset("E0", "2099/2100")

    async def test_unknown_competition_raises(self) -> None:
        with pytest.raises(DatasetNotFoundError):
            await _provider(_csv()).load_dataset("ZZ", "2023/2024")

    @pytest.mark.parametrize(
        ("row", "reason"),
        [
            ("E0,,Arsenal,Chelsea,2,1,1,0,H", RejectionReason.INVALID_DATE),
            ("E0,32/13/2023,Arsenal,Chelsea,2,1,1,0,H", RejectionReason.INVALID_DATE),
            ("E0,12/08/2023,,Chelsea,2,1,1,0,H", RejectionReason.MISSING_TEAM),
            (
                "E0,12/08/2023,Arsenal,Arsenal,2,1,1,0,D",
                RejectionReason.SAME_TEAM_BOTH_SIDES,
            ),
            ("E0,12/08/2023,Arsenal,Chelsea,,,,,", RejectionReason.INVALID_SCORE),
            ("E0,12/08/2023,Arsenal,Chelsea,x,1,1,0,H", RejectionReason.INVALID_SCORE),
            (
                "E0,12/08/2023,Arsenal,Chelsea,99,1,1,0,H",
                RejectionReason.IMPOSSIBLE_SCORE,
            ),
        ],
    )
    async def test_bad_rows_are_rejected_with_a_reason(
        self, row: str, reason: RejectionReason
    ) -> None:
        snapshot = await _provider(_csv(row)).load_dataset("E0", "2023/2024")
        assert snapshot.matches == ()
        assert len(snapshot.rejected) == 1
        assert snapshot.rejected[0].reason is reason
        assert snapshot.rejected[0].row_number == 2
        assert snapshot.rejected[0].detail

    async def test_duplicate_fixtures_are_rejected(self) -> None:
        row = "E0,12/08/2023,Arsenal,Chelsea,2,1,1,0,H"
        snapshot = await _provider(_csv(row, row)).load_dataset("E0", "2023/2024")
        assert len(snapshot.matches) == 1
        assert snapshot.rejected[0].reason is RejectionReason.DUPLICATE_MATCH

    async def test_unplayed_fixture_is_rejected_not_scored_zero(self) -> None:
        """A blank score means unplayed; recording 0-0 would invent a result."""
        snapshot = await _provider(_csv("E0,12/08/2023,Arsenal,Chelsea,,,,,")).load_dataset(
            "E0", "2023/2024"
        )
        assert snapshot.matches == ()

    async def test_latin1_encoding_is_handled(self) -> None:
        """Accented club names break team resolution if mangled."""
        payload = (HEADER + "E0,12/08/2023,Atlético,Chelsea,2,1,1,0,H\n").encode("latin-1")
        snapshot = await _provider(payload).load_dataset("E0", "2023/2024")
        assert snapshot.matches[0].home_team.source_name == "Atlético"

    async def test_decoding_never_fails_but_falls_back_loudly(self) -> None:
        """Latin-1 maps every byte, so decoding cannot raise.

        The consequence is that a mis-encoded file produces mojibake rather
        than an error. That is preferable to discarding a season, but it must
        be logged, and the resulting garbled team name is then caught by the
        resolver's review queue rather than silently mapped.
        """
        text = decode_csv(b"\xff\xfe garbled \xd8\x00")
        assert isinstance(text, str)
        assert text

    async def test_seasons_discovered_from_keys(self) -> None:
        provider = CsvHistoricalDataProvider(
            source=InMemorySource({"E0_2023-2024.csv": _csv(), "E0_2024-2025.csv": _csv()}),
            competitions={"E0": ("Premier League", "England")},
        )
        seasons = await provider.get_seasons("E0")
        assert {s.season for s in seasons} == {"2023/2024", "2024/2025"}


class TestProvenance:
    """An ingestion must remain identifiable after the source changes."""

    async def test_metadata_is_complete(self) -> None:
        payload = _csv("E0,12/08/2023,Arsenal,Chelsea,2,1,1,0,H")
        snapshot = await _provider(payload).load_dataset("E0", "2023/2024")
        meta = snapshot.metadata

        assert meta.content_hash == content_hash(payload)
        assert meta.source_url == "https://example.invalid/E0.csv"
        assert meta.parser_version == "csv-1.0"
        assert meta.schema_version == "football-data-uk-1.0"
        assert meta.row_count == 1
        assert meta.accepted_count == 1
        assert meta.ingested_at.tzinfo is UTC

    async def test_hash_changes_when_source_changes(self) -> None:
        """Revised-in-place sources are detectable even though not recoverable."""
        first = await _provider(_csv("E0,12/08/2023,Arsenal,Chelsea,2,1,1,0,H")).load_dataset(
            "E0", "2023/2024"
        )
        second = await _provider(_csv("E0,12/08/2023,Arsenal,Chelsea,3,1,1,0,H")).load_dataset(
            "E0", "2023/2024"
        )
        assert first.metadata.content_hash != second.metadata.content_hash

    async def test_hash_is_stable_for_identical_bytes(self) -> None:
        payload = _csv("E0,12/08/2023,Arsenal,Chelsea,2,1,1,0,H")
        first = await _provider(payload).load_dataset("E0", "2023/2024")
        second = await _provider(payload).load_dataset("E0", "2023/2024")
        assert first.metadata.content_hash == second.metadata.content_hash

    def test_naive_timestamps_rejected(self) -> None:
        from app.historical.models import HistoricalDatasetMetadata

        with pytest.raises(ValueError, match="timezone"):
            HistoricalDatasetMetadata(
                provider_name="p",
                source_name="s",
                competition_external_id="E0",
                season="2023/2024",
                ingested_at=datetime(2026, 1, 1, 12, 0),
                content_hash="abc",
                row_count=0,
                accepted_count=0,
                rejected_count=0,
                parser_version="v",
                schema_version="v",
            )


class TestArchitecture:
    """Separation between the two provider families must hold."""

    def test_historical_provider_is_not_an_odds_provider(self) -> None:
        from app.providers import OddsProvider

        assert not isinstance(MockHistoricalDataProvider(), OddsProvider)

    def test_historical_layer_does_not_import_orm_models(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "app" / "historical"
        offenders = [p.name for p in root.rglob("*.py") if "app.database.models" in p.read_text()]
        assert offenders == [], f"Historical layer must return DTOs: {offenders}"
