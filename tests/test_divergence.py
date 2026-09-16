"""Model-market divergence tests.

This is the feature most at risk of quietly becoming a value claim. We measured
skill at +0.000% across nine seasons, so a screen implying our disagreements
are right would contradict our own evidence — and would be the single most
damaging thing this product could tell a user.

These tests guard the thresholds that keep it a report rather than a tip, and
the language that keeps it honest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.divergence import (
    MAX_MARKET_PROBABILITY,
    MIN_DIVERGENCE,
    MIN_MODEL_PROBABILITY,
    MIN_RATIO,
    MIN_SAMPLE,
    find_divergences,
)

NOW = datetime(2026, 3, 15, 12, tzinfo=UTC)


def _record(
    fixture_id: str = "1",
    model: tuple[str, str, str] = ("0.52", "0.26", "0.22"),
    market: tuple[str, str, str] = ("0.38", "0.28", "0.34"),
    sample: int = 90,
    hours: int = 5,
) -> SimpleNamespace:
    """Build a stored analysis carrying both views."""
    return SimpleNamespace(
        provider_event_id=fixture_id,
        home_name=f"Alpha{fixture_id}",
        away_name=f"Bravo{fixture_id}",
        competition="Serie A",
        kickoff=NOW + timedelta(hours=hours),
        coverage="fully_modelled",
        model_probabilities={
            "home": model[0],
            "draw": model[1],
            "away": model[2],
        },
        market_probabilities={
            "home": market[0],
            "draw": market[1],
            "away": market[2],
        },
        home_stats={"matches": sample},
        away_stats={"matches": sample},
    )


class TestDetection:
    """What counts as a disagreement worth reporting."""

    def test_finds_a_material_disagreement(self) -> None:
        found = find_divergences([_record()], now=NOW)

        assert len(found) == 1
        assert found[0].outcome == "Home"
        assert found[0].gap >= MIN_DIVERGENCE

    def test_agreement_produces_nothing(self) -> None:
        """Model and market agreeing is the usual state, not a failure."""
        found = find_divergences(
            [_record(model=("0.40", "0.30", "0.30"), market=("0.39", "0.31", "0.30"))],
            now=NOW,
        )
        assert found == []

    def test_small_gaps_are_ignored(self) -> None:
        """Below the threshold the gap is inside the noise of both estimates."""
        found = find_divergences(
            [_record(model=("0.44", "0.28", "0.28"), market=("0.40", "0.30", "0.30"))],
            now=NOW,
        )
        assert found == []

    def test_unlikely_outcomes_are_ignored(self) -> None:
        """A market at 4% against a model at 12% is a large relative
        disagreement about something neither expects."""
        found = find_divergences(
            [_record(model=("0.12", "0.18", "0.70"), market=("0.04", "0.16", "0.80"))],
            now=NOW,
        )
        assert all(item.model_probability >= MIN_MODEL_PROBABILITY for item in found)

    def test_thin_samples_are_ignored(self) -> None:
        """Disagreement from little history is our model not knowing enough,
        not the market being wrong."""
        found = find_divergences([_record(sample=MIN_SAMPLE - 1)], now=NOW)
        assert found == []

    def test_started_fixtures_are_ignored(self) -> None:
        found = find_divergences([_record(hours=-2)], now=NOW)
        assert found == []

    def test_missing_market_prices_are_skipped(self) -> None:
        """Without a real price there is nothing to diverge from, and
        inferring one from our own model would compare it with itself."""
        record = _record()
        record.market_probabilities = {}
        assert find_divergences([record], now=NOW) == []

    def test_missing_model_output_is_skipped(self) -> None:
        record = _record()
        record.model_probabilities = {}
        assert find_divergences([record], now=NOW) == []


class TestPresentation:
    """How a disagreement is described."""

    def test_one_entry_per_fixture(self) -> None:
        """One match listed three times is one disagreement described three
        ways."""
        found = find_divergences([_record("1"), _record("1")], now=NOW)
        assert len({item.fixture_id for item in found}) == len(found)

    def test_ranked_by_size(self) -> None:
        small = _record("small", model=("0.46", "0.28", "0.26"), market=("0.36", "0.30", "0.34"))
        large = _record("large", model=("0.60", "0.22", "0.18"), market=("0.35", "0.30", "0.35"))

        found = find_divergences([small, large], now=NOW)
        assert found[0].fixture_id == "large"

    def test_carries_both_prices(self) -> None:
        """A divergence without the price it diverges from invites a reader to
        assume we think we are right."""
        item = find_divergences([_record()], now=NOW)[0]

        assert item.implied_odds_model > 1
        assert item.implied_odds_market > 1
        assert item.implied_odds_market > item.implied_odds_model

    def test_description_states_both_sides(self) -> None:
        item = find_divergences([_record()], now=NOW)[0]
        description = item.describe()

        assert "we make" in description.lower()
        assert "market" in description.lower()


class TestHonestLanguage:
    """The wording is part of the feature."""

    def test_notice_denies_being_a_value_signal(self) -> None:
        from app.bot.formatting import DIVERGENCE_NOTICE

        assert "not</b> a value signal" in DIVERGENCE_NOTICE

    def test_notice_states_the_models_do_not_beat_prices(self) -> None:
        from app.bot.formatting import DIVERGENCE_NOTICE

        assert "do not" in DIVERGENCE_NOTICE
        assert "closing prices" in DIVERGENCE_NOTICE

    def test_empty_state_does_not_read_as_failure(self) -> None:
        """Agreement is the usual state, and the screen should say so."""
        from app.bot.formatting import format_divergences

        rendered = format_divergences([])
        assert "not a day the product" in rendered

    def test_screen_avoids_value_language(self) -> None:
        """Checked as promotional phrases, not bare words.

        "No outcome is guaranteed" is the disclaimer and must stay; it is
        "guaranteed winner" that would be the lie.
        """
        from app.bot.formatting import format_divergences

        rendered = format_divergences(find_divergences([_record()], now=NOW))
        lowered = rendered.lower()
        for phrase in (
            "value bet",
            "guaranteed win",
            "sure bet",
            "beat the bookmaker",
            "we beat the market",
            "free money",
        ):
            assert phrase not in lowered


class TestOutsiderConstraint:
    """The board must not fill up with favourites.

    The failure this guards against is the one that prompted the rework: a
    side priced at 1.45 appearing on a board whose entire promise is that it
    shows outcomes the market rates as unlikely. A nine-point gap on a
    favourite is a rounding difference, not an upset, and publishing it under
    this heading misleads by framing.
    """

    def test_short_priced_favourite_is_excluded(self) -> None:
        """A market favourite never appears, however much we agree with it."""
        # Market makes home ~69% (about 1.45); we make it 78%. A nine-point
        # gap, but on the shortest price on the card.
        record = _record(
            model=("0.78", "0.13", "0.09"),
            market=("0.69", "0.18", "0.13"),
        )
        assert find_divergences([record], now=NOW) == []

    def test_genuine_outsider_is_reported(self) -> None:
        """An outcome the market prices against, which we rate far higher."""
        record = _record(
            model=("0.40", "0.28", "0.32"),
            market=("0.24", "0.30", "0.46"),
        )
        found = find_divergences([record], now=NOW)
        assert found
        assert found[0].outcome == "Home"
        assert found[0].market_probability < MAX_MARKET_PROBABILITY

    def test_nothing_above_the_market_ceiling_survives(self) -> None:
        """Whatever is returned, the market rated it an outsider."""
        records = [
            _record(fixture_id=str(index), model=model, market=market)
            for index, (model, market) in enumerate(
                [
                    (("0.78", "0.13", "0.09"), ("0.69", "0.18", "0.13")),
                    (("0.40", "0.28", "0.32"), ("0.24", "0.30", "0.46")),
                    (("0.60", "0.22", "0.18"), ("0.50", "0.26", "0.24")),
                    (("0.36", "0.30", "0.34"), ("0.20", "0.32", "0.48")),
                ]
            )
        ]
        for found in find_divergences(records, now=NOW):
            assert found.market_probability <= MAX_MARKET_PROBABILITY

    def test_relative_margin_is_required(self) -> None:
        """A gap that is large in points but small in proportion is dropped."""
        # Market 40%, model 48.5%: 8.5 points, but only 1.21x — below the bar.
        record = _record(
            model=("0.485", "0.265", "0.25"),
            market=("0.40", "0.30", "0.30"),
        )
        found = find_divergences([record], now=NOW)
        assert all(item.ratio >= MIN_RATIO for item in found)

    def test_ceiling_can_be_relaxed_by_the_caller(self) -> None:
        """The bound is configuration, not a hardcoded rule.

        Chosen so the ceiling is the only thing blocking it: the gap and the
        relative margin both clear their bars, so lifting the ceiling alone
        must let it through.
        """
        record = _record(
            model=("0.75", "0.14", "0.11"),
            market=("0.55", "0.23", "0.22"),
        )
        assert find_divergences([record], now=NOW) == []
        assert find_divergences([record], now=NOW, max_market_probability=0.95)

    def test_ratio_is_reported_for_every_entry(self) -> None:
        """The screen shows it, so it must be present and sane."""
        record = _record(
            model=("0.40", "0.28", "0.32"),
            market=("0.24", "0.30", "0.46"),
        )
        for item in find_divergences([record], now=NOW):
            assert item.ratio > 1.0


class TestOutsiderLanguage:
    """The rename must not smuggle in a value claim."""

    def test_board_does_not_promise_profit(self) -> None:
        """Checked as promotional phrases, not bare words.

        The description contains "not value calls", and that denial is the
        point — it is "value bet" that would be the lie.
        """
        from app.services.divergence import BOARD_DESCRIPTION, BOARD_LABEL

        lowered = f"{BOARD_LABEL} {BOARD_DESCRIPTION}".lower()
        for phrase in (
            "value bet",
            "underpriced",
            "guaranteed",
            "profit",
            "free money",
            "beat the book",
        ):
            assert phrase not in lowered

    def test_board_description_admits_the_price_may_be_right(self) -> None:
        """The board's own copy must not imply we know better."""
        from app.services.divergence import BOARD_DESCRIPTION

        assert "may well be right" in BOARD_DESCRIPTION.lower()

    def test_notice_warns_that_outsiders_usually_lose(self) -> None:
        """The honest half of a longshot board, stated on the screen."""
        from app.bot.formatting import DIVERGENCE_NOTICE

        lowered = DIVERGENCE_NOTICE.lower()
        assert "lose more often" in lowered
        assert "variance" in lowered
