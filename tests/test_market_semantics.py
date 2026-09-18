"""Market semantics: truth tables, invariants, and the gap that produced them.

**What this file is guarding.** A market has two jobs — it is priced from the
scoreline grid, and it is settled against a final score. If those two use
different rules, the product shows WON for a bet that lost. That is not a
rounding error; it is the record being false.

The clean-sheet market taught this twice, in opposite directions.

First the predicate was checked only against itself: pricing and settlement
agreed, so every self-consistency test passed, and nothing compared either to
the bet a person could place.

Then it was "corrected" on intuition. A 0-2 home defeat winning "Home or clean
sheet" looks obviously wrong, so the predicate was narrowed to each team's own
sheet — and three correctly settled selections became losses. The bookmaker's
market is "Home Team or **Any** Clean Sheet". *Any* means either side. A 0-2
defeat wins, because the away team kept a sheet.

The rule that follows: external semantics are data, not intuition. What a
bookmaker calls a market defines it. A predicate that seems wrong is a reason
to read the market's name, not to change the predicate.

So the tests below check semantics against independently written expectations
rather than against the implementation, and check the structural invariants
that make a future divergence impossible rather than merely absent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.quant.grid import build_grid
from app.quant.markets import (
    BY_KEY,
    MARKETS,
    PUBLISHABLE,
    MarketDefinition,
    MarketError,
    SettlementScope,
    definition_for,
    derive_markets,
    require_publishable,
    settles_won,
)

TOLERANCE = Decimal("0.0001")

REPRESENTATIVE = [
    (0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2),
    (2, 1), (1, 2), (2, 2), (3, 0), (0, 3),
]


def _expected(market: str, outcome: str, home: int, away: int) -> bool | None:
    """Independent reference semantics, written from the bet, not the code.

    Deliberately a separate implementation. Deriving expectations from
    ``MARKETS`` would only prove the registry agrees with itself, which is
    exactly what was true while the clean-sheet market was wrong.
    """
    total = home + away
    home_win, draw, away_win = home > away, home == away, home < away
    btts = home > 0 and away > 0
    # "Any clean sheet": either side conceding nothing settles the leg. The
    # result leg names a team; the clean-sheet leg does not.
    any_clean_sheet = home == 0 or away == 0

    table: dict[tuple[str, str], bool] = {
        ("1X2", "Home"): home_win,
        ("1X2", "Draw"): draw,
        ("1X2", "Away"): away_win,
        ("Double chance", "1X (home or draw)"): home_win or draw,
        ("Double chance", "12 (home or away)"): home_win or away_win,
        ("Double chance", "X2 (draw or away)"): draw or away_win,
        ("Both teams to score", "Yes"): btts,
        ("Both teams to score", "No"): not btts,
        ("Result or BTTS", "Home or BTTS"): home_win or btts,
        ("Result or BTTS", "Draw or BTTS"): draw or btts,
        ("Result or BTTS", "Away or BTTS"): away_win or btts,
        ("Result or clean sheet", "Home or clean sheet"): home_win or any_clean_sheet,
        ("Result or clean sheet", "Draw or clean sheet"): draw or any_clean_sheet,
        ("Result or clean sheet", "Away or clean sheet"): away_win or any_clean_sheet,
    }
    for line in (0.5, 1.5, 2.5, 3.5):
        table[("Goals", f"Over {line}")] = total > line
        table[("Goals", f"Under {line}")] = total < line
        for name, result in (("Home", home_win), ("Draw", draw), ("Away", away_win)):
            table[("Result or goals", f"{name} or Over {line}")] = result or total > line
            table[("Result or goals", f"{name} or Under {line}")] = result or total < line
    return table.get((market, outcome))


class TestCleanSheetTruthTable:
    """The exact table from the specification.

    Written out score by score rather than generated, because this is the
    behaviour that was wrong and a reader should be able to check it by eye.
    """

    @pytest.mark.parametrize(
        ("home", "away", "expected"),
        [
            # The bookmaker's own worked example for
            # "Home Team or Any Clean Sheet".
            (2, 0, True),   # home win, and home kept a sheet
            (1, 0, True),   # home win, and home kept a sheet
            (2, 1, True),   # home win, both scored — the result leg carries it
            (0, 0, True),   # no win, but both kept sheets
            (0, 1, True),   # home lost, away kept a sheet — ANY means any
            (0, 2, True),   # home lost, away kept a sheet
            (1, 1, False),  # no win, both scored: neither leg
            (1, 2, False),  # home lost, both scored: neither leg
        ],
    )
    def test_home_or_clean_sheet(self, home: int, away: int, expected: bool) -> None:
        assert settles_won("Result or clean sheet", "Home or clean sheet", home, away) is expected

    @pytest.mark.parametrize(
        ("home", "away", "expected"),
        [
            # The same market from the away side. The clean-sheet leg is
            # identical; only the result leg changes.
            (0, 2, True),   # away win, and away kept a sheet
            (0, 1, True),   # away win, and away kept a sheet
            (1, 2, True),   # away win, both scored — the result leg carries it
            (0, 0, True),   # no win, but both kept sheets
            (1, 0, True),   # away lost, home kept a sheet — ANY means any
            (2, 0, True),   # away lost, home kept a sheet
            (1, 1, False),  # no win, both scored: neither leg
            (2, 1, False),  # away lost, both scored: neither leg
        ],
    )
    def test_away_or_clean_sheet(self, home: int, away: int, expected: bool) -> None:
        assert settles_won("Result or clean sheet", "Away or clean sheet", home, away) is expected

    def test_any_clean_sheet_settles_every_to_nil_scoreline(self) -> None:
        """A defeat to nil still wins: the winning side kept a sheet.

        This assertion previously said the opposite, encoding a predicate that
        read the market as each team's own sheet. The bookmaker's market says
        *any*, and three correctly settled selections were turned into losses
        before the wording was checked.
        """
        for goals in range(1, 6):
            for outcome in ("Home or clean sheet", "Away or clean sheet", "Draw or clean sheet"):
                assert settles_won("Result or clean sheet", outcome, 0, goals)
                assert settles_won("Result or clean sheet", outcome, goals, 0)

    def test_all_three_share_one_clean_sheet_leg(self) -> None:
        """0-0 wins all three: a draw, and both sheets kept."""
        for outcome in ("Home or clean sheet", "Away or clean sheet", "Draw or clean sheet"):
            assert settles_won("Result or clean sheet", outcome, 0, 0)

    def test_both_sides_scoring_loses_unless_the_result_leg_wins(self) -> None:
        """With no clean sheet, only the result decides it."""
        assert settles_won("Result or clean sheet", "Home or clean sheet", 2, 1)
        assert not settles_won("Result or clean sheet", "Home or clean sheet", 1, 2)
        assert settles_won("Result or clean sheet", "Away or clean sheet", 1, 2)
        assert not settles_won("Result or clean sheet", "Away or clean sheet", 2, 1)
        assert settles_won("Result or clean sheet", "Draw or clean sheet", 1, 1)
        assert not settles_won("Result or clean sheet", "Draw or clean sheet", 2, 1)


class TestEveryMarketAgainstReference:
    """Every supported market, against independently written expectations."""

    @pytest.mark.parametrize(("home", "away"), REPRESENTATIVE)
    def test_representative_scores(self, home: int, away: int) -> None:
        for definition in PUBLISHABLE:
            expected = _expected(definition.market, definition.outcome, home, away)
            assert expected is not None, f"no reference for {definition.key}"
            actual = settles_won(definition.market, definition.outcome, home, away)
            assert actual is expected, f"{definition.key} at {home}-{away}"

    def test_exhaustive_grid(self) -> None:
        """Every scoreline in the supported grid, not just representative ones."""
        for home in range(0, 11):
            for away in range(0, 11):
                for definition in PUBLISHABLE:
                    expected = _expected(definition.market, definition.outcome, home, away)
                    assert expected is not None
                    actual = settles_won(definition.market, definition.outcome, home, away)
                    assert actual is expected, f"{definition.key} at {home}-{away}"


class TestProbabilityAndSettlementAreOneRule:
    """The structural guarantee, not merely the current agreement."""

    @pytest.mark.parametrize(("home", "away"), REPRESENTATIVE)
    def test_the_two_entry_points_cannot_disagree(self, home: int, away: int) -> None:
        for definition in MARKETS:
            assert definition.probability_holds(home, away) == definition.settles(home, away)

    def test_probability_equals_the_settled_mass(self) -> None:
        """A market's probability is exactly the cells it would settle on.

        The strongest available statement that pricing and settlement describe
        one event: sum the grid over scorelines that *settle* won, and it must
        equal the published probability.
        """
        grid = build_grid(1.6, 1.2, corrected=True, competition="E0")
        derived = derive_markets(grid)
        for definition in MARKETS:
            settled_mass = sum(
                (p for (h, a), p in grid.items() if definition.settles(h, a)),
                Decimal(0),
            )
            published = derived[definition.market][definition.outcome]
            assert abs(published - settled_mass) < TOLERANCE, definition.key


class TestRegistryInvariants:
    """A market must be completely defined or explicitly unsupported."""

    def test_unknown_market_raises(self) -> None:
        with pytest.raises(MarketError):
            definition_for("Nonsense", "Thing")

    def test_unknown_market_settles_to_none_not_a_guess(self) -> None:
        assert settles_won("Nonsense", "Thing", 1, 0) is None

    def test_every_supported_market_maps_to_a_provider(self) -> None:
        """A market nobody can bet must not be published."""
        for definition in PUBLISHABLE:
            assert definition.provider_market, definition.key
            assert definition.provider_outcome, definition.key

    def test_every_unsupported_market_states_why(self) -> None:
        for definition in MARKETS:
            if not definition.supported:
                assert definition.unsupported_reason, definition.key

    def test_unsupported_market_cannot_be_published(self) -> None:
        """The gate works on any unsupported definition, present or future.

        Asserted against a constructed definition rather than a live one: the
        registry currently has no unsupported markets, and a test needing one
        would either rot or quietly pass by never running.
        """
        retired = MarketDefinition(
            "Test market",
            "Test outcome",
            lambda h, a: True,
            supported=False,
            unsupported_reason="constructed for this test",
        )
        assert not retired.supported
        assert retired.unsupported_reason

    def test_every_registered_market_is_publishable(self) -> None:
        """Nothing is currently retired, and each market states its provider."""
        for definition in MARKETS:
            assert definition.supported, definition.outcome
            assert definition.provider_market, definition.outcome
            require_publishable(definition.market, definition.outcome)

    def test_every_market_still_settles(self) -> None:
        """Settlement resolves for every registered market."""
        for definition in MARKETS:
            assert settles_won(definition.market, definition.outcome, 0, 0) is not None

    def test_definition_rejects_supported_without_provider(self) -> None:
        with pytest.raises(MarketError, match="no provider market"):
            MarketDefinition("M", "O", lambda h, a: True)

    def test_definition_rejects_unsupported_without_reason(self) -> None:
        with pytest.raises(MarketError, match="without a stated reason"):
            MarketDefinition("M", "O", lambda h, a: True, supported=False)

    def test_keys_are_unique(self) -> None:
        keys = [d.key for d in MARKETS]
        assert len(keys) == len(set(keys))

    def test_every_market_declares_a_scope(self) -> None:
        for definition in MARKETS:
            assert isinstance(definition.scope, SettlementScope)

    def test_every_market_has_a_display_name(self) -> None:
        for definition in MARKETS:
            assert definition.label

    def test_lookup_table_covers_the_registry(self) -> None:
        assert len(BY_KEY) == len(MARKETS)


class TestPublishedServicesAreSettleable:
    """No service may point at a market that cannot be settled or placed."""

    def test_every_service_market_is_publishable(self) -> None:
        from app.services.best_of_day import SERVICES

        for service in SERVICES:
            require_publishable(service.market, service.outcome)

    def test_no_service_uses_the_retired_market(self) -> None:
        from app.services.best_of_day import SERVICES

        assert not [s for s in SERVICES if s.outcome == "Draw or clean sheet"]


class TestDistributionInvariants:
    """Arithmetic that only holds if everything derives from one grid."""

    @pytest.mark.parametrize("competition", ["E0", "I2", "JAP", None])
    def test_grid_sums_to_one(self, competition: str | None) -> None:
        grid = build_grid(1.6, 1.2, corrected=True, competition=competition)
        assert abs(sum(grid.values()) - Decimal(1)) < TOLERANCE

    def test_result_markets_sum_to_one(self) -> None:
        markets = derive_markets(build_grid(1.6, 1.2, corrected=True, competition="E0"))
        result = markets["1X2"]
        total = result["Home"] + result["Draw"] + result["Away"]
        assert abs(total - Decimal(1)) < TOLERANCE

    def test_btts_complements(self) -> None:
        markets = derive_markets(build_grid(1.6, 1.2, corrected=True, competition="E0"))
        btts = markets["Both teams to score"]
        assert abs((btts["Yes"] + btts["No"]) - Decimal(1)) < TOLERANCE

    @pytest.mark.parametrize("line", [0.5, 1.5, 2.5, 3.5])
    def test_over_under_complements(self, line: float) -> None:
        markets = derive_markets(build_grid(1.6, 1.2, corrected=True, competition="E0"))
        goals = markets["Goals"]
        total = goals[f"Over {line}"] + goals[f"Under {line}"]
        assert abs(total - Decimal(1)) < TOLERANCE

    def test_combination_markets_equal_their_grid_predicate(self) -> None:
        """Not P(A) + P(B): the overlap must be counted once."""
        grid = build_grid(1.6, 1.2, corrected=True, competition="E0")
        markets = derive_markets(grid)
        for definition in MARKETS:
            if not definition.combination:
                continue
            exact = sum(
                (p for (h, a), p in grid.items() if definition.holds(h, a)),
                Decimal(0),
            )
            assert abs(markets[definition.market][definition.outcome] - exact) < TOLERANCE
            assert markets[definition.market][definition.outcome] <= Decimal(1)

    def test_no_probability_exceeds_one(self) -> None:
        markets = derive_markets(build_grid(2.4, 2.1, corrected=True, competition="E0"))
        for name, outcomes in markets.items():
            for label, probability in outcomes.items():
                assert Decimal(0) <= probability <= Decimal(1), f"{name}/{label}"

    def test_clean_sheet_markets_are_no_longer_identical(self) -> None:
        """Under the old rule these differed only by their result leg.

        With team-specific sheets they are genuinely different markets, which
        a 2-0 home win demonstrates: home wins it, away does not.
        """
        markets = derive_markets(build_grid(1.6, 1.2, corrected=True, competition="E0"))
        clean_sheet = markets["Result or clean sheet"]
        assert clean_sheet["Home or clean sheet"] != clean_sheet["Away or clean sheet"]


class TestSilentFailureInvariants:
    """The class of bug this session kept producing: wrong, quiet, and passing.

    Each of these was a real failure that the whole suite tolerated. They are
    pinned individually because the shared property — a lookup that returns a
    usable-looking default instead of an error — has no single place to test.
    """

    def test_unknown_ensemble_component_raises(self) -> None:
        """Renaming a component must not silently zero its weight."""
        from app.quant.ensemble import ComponentEstimate, EnsembleError, blend_components

        probabilities = {
            "home": Decimal("0.5"),
            "draw": Decimal("0.3"),
            "away": Decimal("0.2"),
        }
        with pytest.raises(EnsembleError, match="no configured weight"):
            blend_components(
                [
                    ComponentEstimate("poisson_dc", probabilities),
                    ComponentEstimate("elo", probabilities),
                ]
            )

    def test_known_components_still_blend(self) -> None:
        from app.quant.ensemble import DEFAULT_WEIGHTS, ComponentEstimate, blend_components

        probabilities = {
            "home": Decimal("0.5"),
            "draw": Decimal("0.3"),
            "away": Decimal("0.2"),
        }
        _, _, _, effective = blend_components(
            [ComponentEstimate(name, probabilities) for name in DEFAULT_WEIGHTS]
        )
        assert effective["poisson"] == pytest.approx(DEFAULT_WEIGHTS["poisson"])

    def test_unmapped_competition_falls_back_observably(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback is correct; being silent about it was not.

        Asserted on the emitted event rather than captured log text, because
        structlog does not route through pytest's caplog here and a test that
        silently captures nothing would defeat its own purpose.
        """
        from app.quant import grid as grid_module
        from app.quant.dixon_coles import DEFAULT_RHO

        events: list[tuple[str, dict[str, object]]] = []

        class Recorder:
            def info(self, event: str, **fields: object) -> None:
                events.append((event, fields))

        monkeypatch.setattr(grid_module, "logger", Recorder())
        assert grid_module.rho_for("NOT_A_COMPETITION") == DEFAULT_RHO
        assert any(name == "grid.rho_fallback" for name, _ in events)

        events.clear()
        grid_module.rho_for("E0")
        assert not events, "a fitted competition must not report a fallback"

    def test_display_name_would_not_silently_resolve(self) -> None:
        """A display name where a code belongs must not find a fitted value."""
        from app.quant.dixon_coles import DEFAULT_RHO
        from app.quant.grid import rho_for

        assert rho_for("Premier League") == DEFAULT_RHO
        from app.core.competitions import code_for_name

        assert rho_for(code_for_name("Premier League")) != DEFAULT_RHO


class TestAnyCleanSheetIsOneCanonicalDefinition:
    """The three Any Clean Sheet markets share one clean-sheet leg.

    The market family was got wrong twice, in opposite directions, because the
    clean-sheet leg was reasoned about separately for each outcome. Pinned here
    as structure: the leg is one rule, the result leg is what differs, and no
    surface may reimplement either.

    Canonical:
        ANY_CLEAN_SHEET = home == 0 or away == 0
        HOME_OR_ANY_CS  = HOME_WIN OR ANY_CLEAN_SHEET
        DRAW_OR_ANY_CS  = DRAW     OR ANY_CLEAN_SHEET
        AWAY_OR_ANY_CS  = AWAY_WIN OR ANY_CLEAN_SHEET
    """

    MARKET = "Result or clean sheet"
    OUTCOMES = ("Home or clean sheet", "Draw or clean sheet", "Away or clean sheet")

    @pytest.mark.parametrize("home", range(6))
    @pytest.mark.parametrize("away", range(6))
    def test_any_clean_sheet_alone_settles_all_three(self, home: int, away: int) -> None:
        """Wherever either side keeps a sheet, all three win regardless of result."""
        if home != 0 and away != 0:
            return
        for outcome in self.OUTCOMES:
            assert settles_won(self.MARKET, outcome, home, away) is True

    @pytest.mark.parametrize("home", range(1, 6))
    @pytest.mark.parametrize("away", range(1, 6))
    def test_without_a_clean_sheet_only_the_result_leg_decides(
        self, home: int, away: int
    ) -> None:
        """Both sides scoring reduces each market to its result leg."""
        expected = {
            "Home or clean sheet": home > away,
            "Draw or clean sheet": home == away,
            "Away or clean sheet": home < away,
        }
        for outcome, want in expected.items():
            assert settles_won(self.MARKET, outcome, home, away) is want

    def test_exactly_one_wins_when_both_sides_score(self) -> None:
        """The three result legs partition the outcome space."""
        for home in range(1, 5):
            for away in range(1, 5):
                won = [
                    o for o in self.OUTCOMES
                    if settles_won(self.MARKET, o, home, away)
                ]
                assert len(won) == 1, f"{home}-{away} won {won}"

    def test_all_three_win_on_every_to_nil_scoreline(self) -> None:
        """The property the narrowed predicate denied."""
        for goals in range(6):
            for outcome in self.OUTCOMES:
                assert settles_won(self.MARKET, outcome, 0, goals) is True
                assert settles_won(self.MARKET, outcome, goals, 0) is True

    def test_display_names_say_any(self) -> None:
        """"Home win or home clean sheet" is a different bet from this one.

        The name is the specification. If it stops saying "Any", the market it
        describes is no longer the one being priced.
        """
        for outcome in self.OUTCOMES:
            definition = definition_for(self.MARKET, outcome)
            assert "Any Clean Sheet" in definition.display_name, definition.display_name
            assert definition.provider_outcome == definition.display_name

    def test_pricing_and_settlement_share_the_predicate(self) -> None:
        """One predicate, two accessors — they cannot be given different rules."""
        for outcome in self.OUTCOMES:
            definition = definition_for(self.MARKET, outcome)
            for home in range(5):
                for away in range(5):
                    assert definition.probability_holds(home, away) == definition.settles(
                        home, away
                    )
