"""Phase 8B tests: the backtest runner against synthetic seasons.

The decisive pair is :class:`TestEfficientMarket` and
:class:`TestBeatableMarket`. A harness that only ever says "no edge" is
indistinguishable from a broken one, and a harness that always finds an edge is
worse than useless. These construct both worlds and assert the verdict flips.

Synthetic rather than real data on purpose: the true probabilities are known by
construction, so the harness can be checked against ground truth. Real data
answers whether *this* model beats *that* market; it cannot tell you whether the
measuring instrument works.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.historical.csv_provider import (
    REFERENCE_BOOKMAKER,
    CsvHistoricalDataProvider,
    InMemorySource,
    parse_odds,
)
from app.historical.models import HistoricalMatch, HistoricalTeamRef
from app.quant.backtest_runner import (
    MIN_HISTORY_MATCHES,
    build_forecasts,
    run_backtest,
    sweep_reliability,
)
from app.quant.poisson import match_probabilities
from app.quant.probability import MarginMethod

D = Decimal
SEASON_START = date(2023, 8, 1)

TEAMS: tuple[tuple[str, float, float], ...] = (
    ("Alpha", 1.55, 0.80),
    ("Bravo", 1.40, 0.90),
    ("Charlie", 1.30, 0.95),
    ("Delta", 1.20, 1.00),
    ("Echo", 1.10, 1.05),
    ("Foxtrot", 1.00, 1.10),
    ("Golf", 0.95, 1.15),
    ("Hotel", 0.85, 1.25),
    ("India", 0.80, 1.30),
    ("Juliet", 0.70, 1.45),
)
"""Ten sides with fixed attack and defence multipliers.

Fixed rather than drawn, so the true probability of every fixture is known and
the market can be priced against it exactly.
"""

BASE_HOME_GOALS = 1.45
BASE_AWAY_GOALS = 1.15
OVERROUND = 1.04


def _true_rates(
    home: tuple[str, float, float], away: tuple[str, float, float]
) -> tuple[float, float]:
    """Return the true scoring rates for a fixture."""
    return (
        BASE_HOME_GOALS * home[1] * away[2],
        BASE_AWAY_GOALS * away[1] * home[2],
    )


def _draw_poisson(rate: float, rng: random.Random) -> int:
    """Draw a goal count from a Poisson distribution."""
    limit = math.exp(-rate)
    total = 1.0
    goals = 0
    while True:
        total *= rng.random()
        if total <= limit:
            return goals
        goals += 1
        if goals > 12:
            return goals


def _price(
    probabilities: tuple[float, float, float],
    overround: float,
    distortion: float,
    rng: random.Random,
) -> dict[str, Decimal]:
    """Convert true probabilities into quoted odds.

    Args:
        probabilities: True home, draw and away probabilities.
        overround: Book margin.
        distortion: Standard deviation of multiplicative error applied to the
            book's view. Zero produces a perfectly efficient market.
    """
    view = [max(p * (1 + rng.gauss(0, distortion)), 0.01) for p in probabilities]
    total = sum(view)
    return {
        key: Decimal(str(round(1.0 / ((p / total) * overround), 3)))
        for key, p in zip(("H", "D", "A"), view, strict=True)
    }


def synthetic_season(
    rounds: int = 14,
    distortion: float = 0.0,
    seed: int = 11,
    overround: float = OVERROUND,
) -> list[HistoricalMatch]:
    """Generate matches whose true probabilities are known by construction.

    Args:
        rounds: How many times each pairing is played.
        distortion: Market inefficiency. Zero prices the truth exactly.
        seed: Random seed, so runs are reproducible.
        overround: Bookmaker margin.
    """
    rng = random.Random(seed)
    matches: list[HistoricalMatch] = []
    index = 0

    for _ in range(rounds):
        for home in TEAMS:
            for away in TEAMS:
                if home[0] == away[0]:
                    continue

                lambda_home, lambda_away = _true_rates(home, away)
                home_goals = _draw_poisson(lambda_home, rng)
                away_goals = _draw_poisson(lambda_away, rng)

                truth = match_probabilities(lambda_home, lambda_away)
                quoted = _price(
                    (
                        float(truth.home_win),
                        float(truth.draw),
                        float(truth.away_win),
                    ),
                    overround,
                    distortion,
                    rng,
                )

                played = SEASON_START + timedelta(days=index // 5)
                matches.append(
                    HistoricalMatch(
                        provider_name="synthetic",
                        external_id=f"syn-{index:05d}",
                        competition_external_id="SYN",
                        season="2023/2024",
                        match_date=played,
                        kickoff=datetime(played.year, played.month, played.day, 15, tzinfo=UTC),
                        home_team=HistoricalTeamRef(source_name=home[0]),
                        away_team=HistoricalTeamRef(source_name=away[0]),
                        home_goals=home_goals,
                        away_goals=away_goals,
                        closing_odds={REFERENCE_BOOKMAKER: quoted},
                    )
                )
                index += 1
    return matches


class TestOddsParsing:
    """Bookmaker columns become usable prices."""

    def test_closing_prices_are_preferred(self) -> None:
        """Closing odds are sharper than opening ones."""
        row = {
            "PSH": "2.50",
            "PSD": "3.40",
            "PSA": "3.00",
            "PSCH": "2.10",
            "PSCD": "3.50",
            "PSCA": "3.60",
        }
        assert parse_odds(row)["pinnacle"]["H"] == D("2.10")

    def test_falls_back_to_opening(self) -> None:
        row = {"PSH": "2.50", "PSD": "3.40", "PSA": "3.00"}
        assert parse_odds(row)["pinnacle"]["H"] == D("2.50")

    def test_partial_market_is_ignored(self) -> None:
        """A market missing an outcome cannot have its margin removed."""
        assert parse_odds({"PSH": "2.50", "PSD": "3.40"}) == {}

    def test_impossible_price_is_ignored(self) -> None:
        assert parse_odds({"PSH": "1.00", "PSD": "3.40", "PSA": "3.00"}) == {}

    def test_blank_columns_ignored(self) -> None:
        assert parse_odds({"PSH": "", "PSD": "3.40", "PSA": "3.00"}) == {}

    def test_multiple_bookmakers_extracted(self) -> None:
        """Sharp and soft books from one file is what makes the split work."""
        row = {
            "PSCH": "2.10",
            "PSCD": "3.50",
            "PSCA": "3.60",
            "B365H": "2.00",
            "B365D": "3.40",
            "B365A": "3.50",
        }
        found = parse_odds(row)
        assert set(found) == {"pinnacle", "bet365"}
        assert found["pinnacle"]["H"] > found["bet365"]["H"]

    async def test_parser_reads_odds_from_csv(self) -> None:
        header = "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,PSCH,PSCD,PSCA\n"
        row = "E0,12/08/2023,Arsenal,Chelsea,2,1,2.10,3.50,3.60\n"
        provider = CsvHistoricalDataProvider(
            source=InMemorySource({"E0_2023-2024.csv": (header + row).encode()}),
            competitions={"E0": ("Premier League", "England")},
        )
        snapshot = await provider.load_dataset("E0", "2023/2024")
        assert snapshot.matches[0].odds_for("pinnacle") == (D("2.10"), D("3.50"), D("3.60"))

    def test_odds_for_missing_bookmaker(self) -> None:
        match = synthetic_season(rounds=1)[0]
        assert match.odds_for("nonexistent") is None


class TestPointInTimeDiscipline:
    """A forecast must never see its own result."""

    def test_early_fixtures_are_skipped(self) -> None:
        """Before enough history exists, models return their priors."""
        matches = synthetic_season(rounds=3)
        _, run = build_forecasts(matches)
        assert run.skipped["insufficient_history"] > 0

    def test_skips_are_explained(self) -> None:
        matches = synthetic_season(rounds=3)
        _, run = build_forecasts(matches)
        assert "insufficient_history" in run.summary()

    def test_fixtures_without_odds_are_skipped(self) -> None:
        """No reference price means no prior, so nothing to measure against."""
        matches = [m.model_copy(update={"closing_odds": {}}) for m in synthetic_season(rounds=4)]
        forecasts, run = build_forecasts(matches)
        assert forecasts == []
        assert run.skipped["no_reference_odds"] == run.matches_seen

    def test_three_forecasts_per_fixture(self) -> None:
        forecasts, run = build_forecasts(synthetic_season(rounds=8))
        evaluated = run.matches_seen - sum(run.skipped.values())
        assert len(forecasts) == evaluated * 3

    def test_forecasts_are_chronological(self) -> None:
        forecasts, _ = build_forecasts(synthetic_season(rounds=8))
        times = [f.occurred_at for f in forecasts]
        assert times == sorted(times)

    def test_coverage_is_reported(self) -> None:
        _, run = build_forecasts(synthetic_season(rounds=10))
        assert 0.0 < run.coverage <= 1.0

    def test_history_requirement_is_configurable(self) -> None:
        lenient, run_lenient = build_forecasts(synthetic_season(rounds=6), min_history=2)
        strict, run_strict = build_forecasts(
            synthetic_season(rounds=6), min_history=MIN_HISTORY_MATCHES * 2
        )
        assert len(lenient) > len(strict)
        assert run_lenient.coverage > run_strict.coverage


class TestEfficientMarket:
    """Against a market that prices the truth, the model must not win."""

    def test_model_does_not_beat_an_efficient_market(self) -> None:
        """The honest null result.

        The market here prices the exact probabilities the results were drawn
        from. No model can do better, so a harness reporting an edge would be
        measuring its own error.
        """
        matches = synthetic_season(rounds=14, distortion=0.0)
        report = run_backtest(matches, model_version="vs-efficient", reliability=0.4, min_train=300)
        assert report.result.overall is not None
        assert report.result.overall.brier_skill < 0.01

    def test_gate_refuses_promotion_against_an_efficient_market(self) -> None:
        matches = synthetic_season(rounds=14, distortion=0.0)
        report = run_backtest(matches, model_version="vs-efficient", reliability=0.4, min_train=300)
        assert not report.decision.passed
        assert report.decision.failures

    def test_zero_reliability_is_exactly_the_baseline(self) -> None:
        """With no model influence, the forecast is the market itself."""
        matches = synthetic_season(rounds=12, distortion=0.0)
        report = run_backtest(matches, model_version="baseline", reliability=0.0, min_train=250)
        assert report.result.overall is not None
        assert abs(report.result.overall.brier_skill) < 1e-6


class TestBeatableMarket:
    """Against a distorted market, a working harness must find the edge."""

    def test_model_beats_a_badly_priced_market(self) -> None:
        """The positive control.

        The market's view is distorted away from the truth, so a model
        estimating from actual results carries information the price lacks. A
        harness that cannot detect this would never validate anything.
        """
        matches = synthetic_season(rounds=14, distortion=0.35, seed=5)
        report = run_backtest(matches, model_version="vs-distorted", reliability=0.5, min_train=300)
        assert report.result.overall is not None
        assert report.result.overall.brier_skill > 0

    def test_more_influence_helps_against_a_bad_market(self) -> None:
        """Where the model is right, letting it speak should help."""
        matches = synthetic_season(rounds=14, distortion=0.35, seed=5)
        findings = {
            reliability: brier
            for reliability, brier, _ in sweep_reliability(
                matches, candidates=(0.0, 0.5), min_train=300
            )
        }
        assert findings[0.5] > findings[0.0]

    def test_sweep_baseline_is_zero_by_construction(self) -> None:
        matches = synthetic_season(rounds=12, distortion=0.2)
        findings = sweep_reliability(matches, candidates=(0.0, 0.3), min_train=250)
        baseline = next(b for r, b, _ in findings if r == 0.0)
        assert abs(baseline) < 1e-6


class TestReporting:
    """A run must produce a readable verdict."""

    def test_report_names_the_books_and_method(self) -> None:
        matches = synthetic_season(rounds=12, distortion=0.1)
        report = run_backtest(matches, model_version="v1", reliability=0.3, min_train=250).report()
        assert "pinnacle" in report
        assert "shin" in report
        assert "coverage" in report

    def test_report_states_the_decision(self) -> None:
        matches = synthetic_season(rounds=12, distortion=0.0)
        report = run_backtest(matches, model_version="v1", reliability=0.3, min_train=250).report()
        assert "PROMOTED" in report

    def test_margin_method_is_configurable(self) -> None:
        matches = synthetic_season(rounds=12, distortion=0.1)
        for method in (MarginMethod.PROPORTIONAL, MarginMethod.SHIN):
            report = run_backtest(
                matches,
                model_version="v1",
                reliability=0.3,
                margin_method=method,
                min_train=250,
            )
            assert report.margin_method == str(method)

    def test_too_little_data_raises_with_context(self) -> None:
        with pytest.raises(ValueError, match="not enough to evaluate"):
            run_backtest(synthetic_season(rounds=1), model_version="v1", min_train=300)
