"""Competition scope: the substring bugs that kept recurring.

Three separate times a competition was misclassified because a term was
matched as a substring, or because a name carried no term at all:

* "USL Championship" matched "champions" and became a continental competition.
* "Toppserien" matched nothing and joined the expansion candidates.
* "Liga MX Femenil" matched neither "feminin" nor "femenin" — Spanish drops
  the second "i" — and reached a research queue of senior men's leagues.

Each was fixed in one script and repeated in the next. These tests pin the
shared classifier so the fix travels with it.
"""

from __future__ import annotations

import pytest

from app.core.competition_scope import Scope, classify_scope, is_in_scope


class TestWomensCompetitions:
    """Multilingual, because the naming is not English."""

    @pytest.mark.parametrize(
        ("name", "country"),
        [
            ("Liga MX Femenil", "Mexico"),          # the bug
            ("Primera Division Femenina", "Spain"),
            ("Division 1 Feminine", "France"),
            ("Serie A Femminile", "Italy"),
            ("Frauen-Bundesliga", "Germany"),
            ("FA Women's Super League", "England"),
            ("Eredivisie Vrouwen", "Netherlands"),
            ("Kvinnor Elitettan", "Sweden"),
            ("Kadinlar Ligi", "Turkey"),
            ("Ekstraliga Kobiet", "Poland"),
            ("Campeonato Feminino", "Brazil"),
            ("Serie A W", "Italy"),
        ],
    )
    def test_womens_names_are_detected(self, name: str, country: str) -> None:
        assert classify_scope(name, country, "League") is Scope.WOMENS

    @pytest.mark.parametrize("league_id", [725, 724, 673, 44])
    def test_womens_leagues_without_clues_use_the_id(self, league_id: int) -> None:
        """Toppserien and Damallsvenskan give nothing away by name."""
        assert (
            classify_scope("Toppserien", "Norway", "League", league_id) is Scope.WOMENS
        )

    def test_femenil_specifically(self) -> None:
        """Pinned on its own: this reached a live research queue."""
        assert classify_scope("Liga MX Femenil", "Mexico", "League", 673) is Scope.WOMENS
        assert not is_in_scope(classify_scope("Liga MX Femenil", "Mexico", "League"))


class TestSubstringSafety:
    """Whole-word matching, so one term cannot hide inside another."""

    def test_championship_is_not_champions(self) -> None:
        """A domestic American league, filed as continental by a substring."""
        assert classify_scope("USL Championship", "USA", "League") is Scope.SENIOR_MENS_LEAGUE

    @pytest.mark.parametrize(
        "name",
        [
            "Championship",
            "National League",
            "Premier League",
            "Women",  # only as its own word
        ],
    )
    def test_league_names_are_not_swallowed_by_hints(self, name: str) -> None:
        scope = classify_scope(name, "England", "League")
        assert scope in {Scope.SENIOR_MENS_LEAGUE, Scope.WOMENS}

    def test_caf_does_not_match_inside_a_word(self) -> None:
        assert classify_scope("Cafetaleros League", "Mexico", "League") is (
            Scope.SENIOR_MENS_LEAGUE
        )


class TestYouthAndReserve:
    @pytest.mark.parametrize(
        "name",
        [
            "Premier League 2 Division One",
            "U21 Bundesliga",
            "Campeonato Juvenil",
            "Primavera 1",
            "Reserve League",
            "Liga Revelacao Sub23",
            "Jugend Liga",
        ],
    )
    def test_youth_and_reserve_are_out_of_scope(self, name: str) -> None:
        assert not is_in_scope(classify_scope(name, "Germany", "League"))


class TestInternational:
    @pytest.mark.parametrize(
        "name",
        [
            "UEFA Europa League",
            "CONMEBOL Libertadores",
            "CONCACAF Central American Cup",
            "AFC Champions League Two",
            "CAF Confederation Cup",
        ],
    )
    def test_continental_club_competitions(self, name: str) -> None:
        assert classify_scope(name, "World", "Cup") is Scope.INTERNATIONAL_CLUB

    @pytest.mark.parametrize(
        "name", ["World Cup - Qualification", "Nations League", "Asian Games", "Friendlies"]
    )
    def test_national_team_competitions(self, name: str) -> None:
        assert classify_scope(name, "World", "Cup") is Scope.INTERNATIONAL_NATIONAL


class TestDomesticCups:
    @pytest.mark.parametrize(
        ("name", "country"),
        [
            ("FA Cup", "England"),
            ("Türkiye Kupası", "Turkey"),
            ("Copa Uruguay", "Uruguay"),
            ("Taça de Portugal", "Portugal"),
            ("DBU Pokalen", "Denmark"),
        ],
    )
    def test_cups_are_out_of_scope(self, name: str, country: str) -> None:
        """A cup spans divisions, so its scoring environments are not comparable."""
        assert classify_scope(name, country, "Cup") is Scope.SENIOR_MENS_CUP
        assert not is_in_scope(classify_scope(name, country, "Cup"))


class TestUnknownRatherThanAssumed:
    """An unmatched competition is never assumed to be a senior men's league."""

    def test_no_catalogue_type_yields_unknown(self) -> None:
        assert classify_scope("Something Unfamiliar", "Georgia", None) is Scope.UNKNOWN

    def test_in_scope_requires_a_senior_mens_league(self) -> None:
        for scope in Scope:
            assert is_in_scope(scope) is (scope is Scope.SENIOR_MENS_LEAGUE)

    def test_known_candidates_remain_in_scope(self) -> None:
        """The competitions the research queue surfaced must survive."""
        for name, country in (
            ("Serie C - Girone A", "Italy"),
            ("Liga Leumit", "Israel"),
            ("Azadegan League", "Iran"),
            ("Prva Liga", "Serbia"),
            ("Liga Pro Serie B", "Ecuador"),
        ):
            assert is_in_scope(classify_scope(name, country, "League")), name
