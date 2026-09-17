"""The canonical grid: routing, consistency and the correction itself.

Two properties are under test here, and they fail in different ways.

**Routing.** Every production path must build its distribution through
:mod:`app.quant.grid`. The failure mode is silent: a path that kept its own
Poisson import keeps working, keeps passing its own tests, and quietly serves
a different probability for the same fixture than the screen next to it. So
these tests read the source of each production module and assert on its
imports, which is the only way to catch a path that was never rerouted.

**Derivation.** Every market must be a sum over one grid. The failure mode is
again silent: a separately computed BTTS would still look plausible while
contradicting the scoreline distribution it is supposed to come from. The
consistency tests below pin the arithmetic that makes that impossible.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from app.quant import grid as grid_module
from app.quant.dixon_coles import DEFAULT_RHO
from app.quant.grid import (
    GRID_VERSION_CORRECTED,
    GRID_VERSION_INDEPENDENT,
    MODEL_NAME_CORRECTED,
    MODEL_NAME_INDEPENDENT,
    build_grid,
    build_match_probabilities,
    correction_enabled,
    grid_version,
    model_name,
)
from app.quant.markets import derive_markets
from app.quant.poisson import score_matrix as independent_matrix

TOLERANCE = Decimal("0.0001")


class TestProductionRouting:
    """No production path may build its own distribution.

    Asserted against module source rather than behaviour, because a path that
    bypasses the builder produces perfectly reasonable numbers — just not the
    same ones as everything else.
    """

    @pytest.mark.parametrize(
        "module_path",
        [
            "app.services.match_analysis",
            "app.services.selections",
            "app.services.market_history",
            "app.quant.markets",
        ],
    )
    def test_module_does_not_import_a_raw_engine(self, module_path: str) -> None:
        """Production modules import the builder, never the engines."""
        module = __import__(module_path, fromlist=["__file__"])
        source = inspect.getsource(module)
        assert "from app.quant.poisson import score_matrix" not in source
        assert "from app.quant.dixon_coles import score_matrix" not in source
        assert "from app.quant.grid import" in source

    def test_backtest_runner_keeps_both_engines(self) -> None:
        """The one legitimate exception.

        The harness exists to score the corrected and uncorrected models
        against each other on identical fixtures. Routing it through the
        builder would make it compare a model with itself.
        """
        import app.quant.backtest_runner as runner

        source = inspect.getsource(runner)
        assert "dixon_coles" in source
        assert "match_probabilities" in source


class TestCorrectionSwitch:
    """The correction is configurable and defaults on."""

    def test_defaults_to_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On by default, on the evidence in docs/VALIDATION.md sections 13-18.

        Pinned because the default is a decision the holdout audit made, not a
        convenience. It was off for the global rho and earned being on only
        once rho was fitted per competition.
        """
        monkeypatch.delenv(grid_module.CORRECTION_ENV, raising=False)
        assert correction_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
    def test_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(grid_module.CORRECTION_ENV, value)
        assert correction_enabled() is False

    def test_disabled_reproduces_the_previous_engine_exactly(self) -> None:
        """Turning it off must be a true rollback, not an approximation."""
        corrected_off = build_grid(1.7, 1.2, corrected=False)
        previous = independent_matrix(1.7, 1.2)
        assert corrected_off == previous

    def test_version_and_name_follow_the_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(grid_module.CORRECTION_ENV, raising=False)
        assert model_name() == MODEL_NAME_CORRECTED
        assert grid_version() == GRID_VERSION_CORRECTED
        monkeypatch.setenv(grid_module.CORRECTION_ENV, "0")
        assert model_name() == MODEL_NAME_INDEPENDENT
        assert grid_version() == GRID_VERSION_INDEPENDENT

    def test_malformed_rho_falls_back_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad environment variable must not take the scanner down."""
        monkeypatch.setenv(grid_module.RHO_ENV, "not-a-number")
        assert grid_module.active_rho() == DEFAULT_RHO


class TestGridIsADistribution:
    """Whatever the settings, the output is a probability distribution."""

    @pytest.mark.parametrize("corrected", [True, False])
    @pytest.mark.parametrize(
        ("lambda_home", "lambda_away"),
        [(0.4, 0.3), (1.2, 1.1), (1.8, 0.9), (3.2, 2.7), (0.05, 4.0)],
    )
    def test_sums_to_one(
        self, lambda_home: float, lambda_away: float, corrected: bool
    ) -> None:
        total = sum(build_grid(lambda_home, lambda_away, corrected=corrected).values())
        assert abs(total - Decimal(1)) < TOLERANCE

    @pytest.mark.parametrize("corrected", [True, False])
    def test_no_negative_probabilities(self, corrected: bool) -> None:
        """A large rho against high rates can drive tau negative if unguarded."""
        for probability in build_grid(3.5, 3.1, corrected=corrected).values():
            assert probability >= 0


class TestMarketConsistency:
    """Every market is a sum over the same grid.

    One fixture, one distribution, every market derived from it. These are the
    arithmetic identities that hold only if nothing is computed separately.
    """

    @pytest.mark.parametrize("corrected", [True, False])
    def test_result_markets_partition_the_grid(self, corrected: bool) -> None:
        p = build_match_probabilities(1.6, 1.1, corrected=corrected)
        assert abs((p.home_win + p.draw + p.away_win) - Decimal(1)) < TOLERANCE

    @pytest.mark.parametrize("corrected", [True, False])
    def test_over_and_under_are_complements(self, corrected: bool) -> None:
        p = build_match_probabilities(1.6, 1.1, corrected=corrected)
        for line in (0.5, 1.5, 2.5, 3.5):
            over = p.over_under[f"over_{line}"]
            under = p.over_under[f"under_{line}"]
            assert abs((over + under) - Decimal(1)) < TOLERANCE

    @pytest.mark.parametrize("corrected", [True, False])
    def test_over_lines_are_monotonic(self, corrected: bool) -> None:
        """A higher line cannot be likelier to be exceeded."""
        p = build_match_probabilities(1.6, 1.1, corrected=corrected)
        overs = [p.over_under[f"over_{line}"] for line in (0.5, 1.5, 2.5, 3.5)]
        assert overs == sorted(overs, reverse=True)

    @pytest.mark.parametrize("corrected", [True, False])
    def test_double_chance_equals_the_sum_of_its_legs(self, corrected: bool) -> None:
        """Double chance is derived, never modelled separately."""
        markets = derive_markets(build_grid(1.6, 1.1, corrected=corrected))
        result = markets["1X2"]
        chance = markets["Double chance"]
        pairs = [
            ("1X (home or draw)", ("Home", "Draw")),
            ("12 (home or away)", ("Home", "Away")),
            ("X2 (draw or away)", ("Draw", "Away")),
        ]
        for label, legs in pairs:
            expected = sum((result[leg] for leg in legs), Decimal(0))
            assert abs(chance[label] - expected) < TOLERANCE

    @pytest.mark.parametrize("corrected", [True, False])
    def test_btts_agrees_between_builder_and_derivation(self, corrected: bool) -> None:
        """The headline figure and the derived market are the same number."""
        p = build_match_probabilities(1.6, 1.1, corrected=corrected)
        markets = derive_markets(build_grid(1.6, 1.1, corrected=corrected))
        assert abs(p.both_teams_score - markets["Both teams to score"]["Yes"]) < TOLERANCE

    @pytest.mark.parametrize("corrected", [True, False])
    def test_every_derived_market_is_a_distribution(self, corrected: bool) -> None:
        markets = derive_markets(build_grid(1.7, 1.3, corrected=corrected))
        for name, outcomes in markets.items():
            for label, probability in outcomes.items():
                assert Decimal(0) <= probability <= Decimal(1), f"{name}/{label}"


class TestCorrectionDirection:
    """The correction must move draws the way the theory says.

    Not a claim that it makes the model right — only that it does what it was
    added to do. A correction applied in the wrong direction would deepen the
    bias it was meant to fix.
    """

    @pytest.mark.parametrize(
        ("lambda_home", "lambda_away"),
        [(1.2, 1.1), (1.5, 1.3), (1.8, 1.0), (0.9, 0.8)],
    )
    def test_draw_probability_rises(self, lambda_home: float, lambda_away: float) -> None:
        plain = build_match_probabilities(lambda_home, lambda_away, corrected=False)
        fixed = build_match_probabilities(lambda_home, lambda_away, corrected=True)
        assert fixed.draw > plain.draw

    def test_only_low_scores_are_touched(self) -> None:
        """Nothing above one goal each may move."""
        plain = build_grid(1.6, 1.2, corrected=False)
        fixed = build_grid(1.6, 1.2, corrected=True)
        # Renormalisation shifts everything slightly; the four corrected cells
        # must move far more than the untouched ones.
        untouched = max(
            abs(fixed[(h, a)] - plain[(h, a)])
            for h in range(2, 6)
            for a in range(2, 6)
        )
        corrected_cells = min(
            abs(fixed[cell] - plain[cell]) for cell in [(0, 0), (0, 1), (1, 0), (1, 1)]
        )
        assert corrected_cells > untouched

    def test_result_ordering_is_preserved(self) -> None:
        """A clear favourite stays the favourite."""
        plain = build_match_probabilities(2.1, 0.8, corrected=False)
        fixed = build_match_probabilities(2.1, 0.8, corrected=True)
        assert plain.home_win > plain.away_win
        assert fixed.home_win > fixed.away_win


class TestVersionStamping:
    """A record spanning a model change must be able to tell the sides apart."""

    def test_versions_are_distinct(self) -> None:
        assert GRID_VERSION_CORRECTED != GRID_VERSION_INDEPENDENT

    def test_published_version_follows_the_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.best_of_day import (
            MODEL_ONLY_VERSION,
            MODEL_ONLY_VERSION_DC,
            model_only_version,
        )

        monkeypatch.delenv(grid_module.CORRECTION_ENV, raising=False)
        assert model_only_version() == MODEL_ONLY_VERSION_DC
        monkeypatch.setenv(grid_module.CORRECTION_ENV, "0")
        assert model_only_version() == MODEL_ONLY_VERSION


class TestEnsembleWeightingSurvives:
    """Guards the regression found while wiring this in.

    Ensemble weights are keyed on the component's name. Naming the Poisson
    component after the engine — "poisson_dc" — resolves to a configured
    weight of zero and silently hands its entire share to Elo and Form. The
    whole suite passed with that bug in place, so it is pinned here.
    """

    def test_poisson_component_keeps_its_configured_weight(self) -> None:
        from app.quant.ensemble import DEFAULT_WEIGHTS, ComponentEstimate, blend_components

        probabilities = {
            "home": Decimal("0.5"),
            "draw": Decimal("0.3"),
            "away": Decimal("0.2"),
        }
        components = [
            ComponentEstimate("poisson", probabilities),
            ComponentEstimate("elo", probabilities),
            ComponentEstimate("form", probabilities),
        ]
        _, _, _, effective = blend_components(components)
        assert effective["poisson"] == pytest.approx(DEFAULT_WEIGHTS["poisson"])

    def test_live_analysis_names_the_component_poisson(self) -> None:
        """Asserted on source: the name is load-bearing for weighting."""
        import app.services.match_analysis as analysis

        source = inspect.getsource(analysis)
        assert 'ComponentEstimate(\n' in source
        assert '"poisson"' in source
        assert "model_name()," not in source


class TestPerCompetitionRho:
    """The fitted table, and the fallbacks around it."""

    def test_fitted_competitions_differ_from_the_default(self) -> None:
        """If every lookup returned the default, the table is not wired in."""
        from app.quant.dixon_coles import DEFAULT_RHO
        from app.quant.grid import rho_for

        fitted = [rho_for(code) for code in ("E0", "I2", "JAP", "SWE")]
        assert any(value != DEFAULT_RHO for value in fitted)

    def test_unknown_competition_falls_back(self) -> None:
        from app.quant.dixon_coles import DEFAULT_RHO
        from app.quant.grid import rho_for

        assert rho_for("NOT_A_LEAGUE") == DEFAULT_RHO
        assert rho_for(None) == DEFAULT_RHO

    def test_environment_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.quant.grid import rho_for

        monkeypatch.setenv(grid_module.RHO_ENV, "-0.09")
        assert rho_for("E0") == pytest.approx(-0.09)

    def test_fitted_values_stay_inside_the_search_bounds(self) -> None:
        """A value outside the bounds means a corrupt or hand-edited table."""
        from app.quant.dixon_coles import RHO_BOUNDS
        from app.quant.grid import _rho_table

        low, high = RHO_BOUNDS
        for code, value in _rho_table().items():
            assert low <= value <= high, f"{code} rho {value} outside {RHO_BOUNDS}"

    def test_competition_changes_the_distribution(self) -> None:
        """Passing a competition must actually reach the correction."""
        from app.quant.grid import build_match_probabilities as build

        japan = build(1.5, 1.2, corrected=True, competition="JAP")
        italy = build(1.5, 1.2, corrected=True, competition="I2")
        assert japan.draw != italy.draw

    def test_positive_rho_lowers_draws(self) -> None:
        """Three competitions fit positive; the effect must run the other way.

        Brazil, Japan and Scottish League Two score closer to independently
        than Poisson assumes. Applying the negative default there added draw
        mass that was not missing, which is the +2.7% overshoot round 1 saw.
        """
        from app.quant.grid import build_match_probabilities as build
        from app.quant.grid import rho_for

        assert rho_for("JAP") > 0
        plain = build(1.5, 1.2, corrected=False)
        japan = build(1.5, 1.2, corrected=True, competition="JAP")
        assert japan.draw < plain.draw

    def test_table_is_read_once(self) -> None:
        """Cached: this is read on every fixture in a scan."""
        from app.quant.grid import _rho_table

        assert _rho_table() is _rho_table()


class TestLivePathResolvesRho:
    """The live analysis must reach the fitted table, not the default.

    The failure this guards is silent and total: analyses carry a competition
    *name* while the fitted table is keyed by *code*. Passing the name through
    unmapped resolves nothing, every fixture takes the global default, and the
    holdout result this was adopted on quietly does not apply in production.
    """

    def test_display_names_resolve_to_codes(self) -> None:
        from app.core.competitions import code_for_name

        assert code_for_name("Premier League") == "E0"
        assert code_for_name("premier league") == "E0"
        assert code_for_name("Nonsense League") is None
        assert code_for_name(None) is None

    def test_resolved_names_reach_a_fitted_parameter(self) -> None:
        from app.core.competitions import code_for_name
        from app.quant.dixon_coles import DEFAULT_RHO
        from app.quant.grid import rho_for

        resolved = [rho_for(code_for_name(n)) for n in ("Premier League", "Serie B")]
        assert any(value != DEFAULT_RHO for value in resolved)

    def test_live_analysis_maps_the_name(self) -> None:
        """Asserted on source: passing the raw name would be a silent no-op."""
        import app.services.match_analysis as analysis

        source = inspect.getsource(analysis)
        assert "code_for_name(analysis.competition)" in source
