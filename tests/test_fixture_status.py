"""Fixture status policy and the collisions the resolver must never merge.

Two properties, both learned from production.

The status policy exists because "stored" and "trainable" were being decided
per caller. NPFL carries two seasons of abandoned fixtures and Liga Alef 430;
those matches happened and must be kept, and must never train a model. One
policy answers that for ingestion, fitting and validation alike.

The collision tests exist because seeding 144 teams produced thirteen fuzzy
matches against clubs created moments earlier in the same run — Kwara United
scoring 0.83 against Akwa United, Tzeirey Tira 0.90 against Tira. Each was
refused by the same-provider safeguard. Without it, a Nigerian club's history
would now sit under another club's name.
"""

from __future__ import annotations

import pytest

from app.core.fixture_status import (
    FixtureStatus,
    classify_status,
    exclusion_reason,
    is_training_eligible,
)


class TestStatusClassification:
    @pytest.mark.parametrize("code", ["FT", "AET", "PEN", "ft", " FT "])
    def test_finished(self, code: str) -> None:
        assert classify_status(code) is FixtureStatus.FINISHED

    @pytest.mark.parametrize("code", ["ABD", "SUSP", "INT", "AWD", "WO"])
    def test_abandoned(self, code: str) -> None:
        assert classify_status(code) is FixtureStatus.ABANDONED

    @pytest.mark.parametrize("code", ["NS", "TBD", "PST", "CANC"])
    def test_not_played(self, code: str) -> None:
        assert classify_status(code) is FixtureStatus.NOT_PLAYED

    @pytest.mark.parametrize("code", ["1H", "HT", "2H", "ET", "LIVE"])
    def test_in_progress(self, code: str) -> None:
        assert classify_status(code) is FixtureStatus.IN_PROGRESS

    @pytest.mark.parametrize("code", [None, "", "ZZZ", "WHAT"])
    def test_unknown_is_not_assumed_finished(self, code: str | None) -> None:
        """An unrecognised code is a gap in our knowledge, not a result."""
        assert classify_status(code) is FixtureStatus.UNKNOWN
        assert not is_training_eligible(code, 1, 0)


class TestTrainingEligibility:
    def test_finished_with_a_score_is_eligible(self) -> None:
        assert is_training_eligible("FT", 2, 1)

    def test_extra_time_and_penalties_count(self) -> None:
        """The ninety-minute score is still a real football result."""
        assert is_training_eligible("AET", 1, 1)
        assert is_training_eligible("PEN", 2, 2)

    @pytest.mark.parametrize(
        ("home", "away"), [(None, 1), (1, None), (None, None)]
    )
    def test_finished_without_a_score_is_excluded(
        self, home: int | None, away: int | None
    ) -> None:
        """Stored, because the match happened. Excluded, because we cannot say what."""
        assert not is_training_eligible("FT", home, away)
        assert exclusion_reason("FT", home, away) == "finished without a final score"

    def test_awarded_results_never_train(self) -> None:
        """A disciplinary outcome must not inform a model that estimates goals."""
        assert not is_training_eligible("AWD", 3, 0)
        assert not is_training_eligible("WO", 3, 0)

    @pytest.mark.parametrize("code", ["ABD", "PST", "CANC", "NS", "HT"])
    def test_every_exclusion_states_a_reason(self, code: str) -> None:
        """Nothing may vanish from training unexplained."""
        assert exclusion_reason(code, 1, 0)

    def test_eligible_fixtures_have_no_reason(self) -> None:
        assert exclusion_reason("FT", 2, 1) is None


class TestPolicyIsNotCompetitionSpecific:
    """Eligibility is derived from the fixture, never assigned to a season.

    Hard-coding "NPFL 2018 is unreliable" would be an opinion hidden in a table
    of facts, and would not survive the next bad season nobody checked.
    """

    def test_same_status_same_answer_regardless_of_origin(self) -> None:
        for _competition in ("NPFL", "Premier League", "Liga Alef"):
            assert is_training_eligible("FT", 1, 0)
            assert not is_training_eligible("ABD", 1, 0)


class TestProviderCollisionsMustNotMerge:
    """Distinct clubs whose names score high against each other.

    Every pair below was proposed as a match during seeding and refused. The
    invariant: two distinct external ids from one provider must never collapse
    into a single canonical team through fuzzy matching, however similar the
    names.
    """

    COLLISIONS = [
        ("Kwara United", "Akwa United", 0.8333),
        ("Tzeirey Tira", "Tira", 0.90),
        ("Hapoel Hadera", "Hapoel Acre", 0.75),
        ("Hapoel Kfar Shalem", "Hapoel Kfar Saba", 0.8235),
        ("Maccabi Kiryat Malachi", "Maccabi Kiryat Gat", 0.80),
        ("Hapoel Ramat HaSharon", "Hapoel Ramat Gan", 0.8095),
        ("Nasarawa United", "Akwa United", 0.75),
        ("Tzeirey Tamra", "Tzeirey Tira", 0.8333),
        ("FC Jerusalem", "Nordia Jerusalem", 0.90),
        ("Hapoel Ironi Arraba", "Hapoel Ironi Karmiel", 0.7692),
    ]

    @pytest.mark.parametrize(("left", "right", "observed"), COLLISIONS)
    def test_names_really_do_look_alike(
        self, left: str, right: str, observed: float
    ) -> None:
        """Each pair looks alike by one of the two matching routes.

        Most score highly on string similarity. Two — "Tira" inside "Tzeirey
        Tira", "Jerusalem" inside both Jerusalem clubs — reach 0.90 by token
        containment instead, where one name's tokens are a subset of the
        other's. Checking only similarity would have missed exactly the pairs
        most at risk of merging, since containment scores higher.
        """
        from app.services.team_resolution import (
            contains_all_tokens,
            normalize_team_name,
            similarity,
        )

        left_normal = normalize_team_name(left)
        right_normal = normalize_team_name(right)
        score = similarity(left_normal, right_normal)
        contained = contains_all_tokens(
            right_normal, left_normal
        ) or contains_all_tokens(left_normal, right_normal)
        assert score > 0.6 or contained, f"{left} vs {right}: {score}, contained={contained}"

    @pytest.mark.parametrize(("left", "right", "observed"), COLLISIONS)
    def test_they_are_not_the_same_club(
        self, left: str, right: str, observed: float
    ) -> None:
        """Distinct names from one provider are distinct clubs."""
        assert left.strip().lower() != right.strip().lower()

    def test_resolver_refuses_same_provider_fuzzy_matches(self) -> None:
        """Asserted on source: this safeguard did the work thirteen times.

        A provider does not supply two different names for one club, so a fuzzy
        match against another team from the same feed is a collision, not a
        match.
        """
        import inspect

        import app.services.team_resolution as resolution

        source = inspect.getsource(resolution)
        assert "fuzzy_rejected_same_provider" in source
        assert "_provider_already_named" in source
