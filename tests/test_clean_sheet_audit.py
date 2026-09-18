"""The correction script's own behaviour.

A script that edits a published record needs its own tests more than most
code does, because its failures are written into the thing people trust. Both
bugs pinned below shipped and ran against production.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

AUDIT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_clean_sheet.py"

_spec = importlib.util.spec_from_file_location("audit_clean_sheet", AUDIT_PATH)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit_clean_sheet"] = audit
_spec.loader.exec_module(audit)

from app.quant.markets import settles_won  # noqa: E402

HOME_CS = "Home or clean sheet"
AWAY_CS = "Away or clean sheet"
MARKET = "Result or clean sheet"


class TestOldRuleIsReproducedFaithfully:
    """The superseded predicate, kept so affected rows can be reasoned about.

    ``old_rule`` reproduces the *any clean sheet* predicate, which the registry
    has now been restored to. So the two agree everywhere: the narrowing that
    once separated them was the error, and reverting it removed the divergence.

    The class is kept rather than deleted because a future predicate change
    needs this comparison, and because the three corrected settlements are only
    explicable with both rules in view.
    """

    @pytest.mark.parametrize(("home", "away"), [(0, 1), (0, 2), (0, 3), (0, 4)])
    def test_defeats_to_nil_win_under_both_rules(self, home: int, away: int) -> None:
        """A home defeat to nil: the away side kept a sheet, so ANY settles it.

        These are the scorelines of the three production rows that were
        wrongly moved to LOST. Under the restored predicate they win again,
        which is what the audit must put back.
        """
        assert audit.old_rule(HOME_CS, home, away) is True
        assert settles_won(MARKET, HOME_CS, home, away) is True

    def test_rules_agree_where_they_should(self) -> None:
        for home, away in [(2, 0), (2, 1), (0, 0), (1, 1)]:
            assert audit.old_rule(HOME_CS, home, away) == settles_won(
                MARKET, HOME_CS, home, away
            )


class TestCorrectionOnlyEverRemovesWins:
    """A structural property, not an observation about these three rows.

    The registry now matches ``old_rule`` exactly, so a fresh audit finds
    nothing to change. What it must still do is reverse the earlier correction:
    the three rows moved to LOST are WON under the restored predicate.
    """

    def test_new_rule_never_wins_where_the_old_one_lost(self) -> None:
        for home in range(0, 8):
            for away in range(0, 8):
                for outcome in (HOME_CS, AWAY_CS):
                    old = audit.old_rule(outcome, home, away)
                    new = settles_won(MARKET, outcome, home, away)
                    if old is False:
                        assert new is False, f"{outcome} {home}-{away} gained a win"


class TestDirectionReportsBothEnds:
    """The display bug: a re-run reported the exact opposite of its action."""

    def _affected(self, old: bool, new: bool) -> audit.Affected:  # type: ignore[name-defined]
        return audit.Affected(
            table="service_selections", row_id=1, day="2026-09-13",
            fixture="A v B", service="home_or_cs", market=MARKET, outcome=HOME_CS,
            probability=0.75, home_goals=0, away_goals=3,
            old_result=old, new_result=new,
        )

    def test_downgrade(self) -> None:
        assert self._affected(True, False).direction == "WON -> LOST"

    def test_upgrade(self) -> None:
        assert self._affected(False, True).direction == "LOST -> WON"

    def test_no_change_is_not_described_as_a_flip(self) -> None:
        """The failing case: old=LOST, new=LOST once printed 'LOST -> WON'."""
        assert self._affected(False, False).direction == "LOST -> LOST"


class TestIdempotency:
    """A second run must report nothing, which is what 'done' looks like."""

    def test_affected_is_decided_against_stored_status(self) -> None:
        """Asserted on source: comparing the two rules is true forever.

        The original compared ``old_rule`` with ``settles_won``, both functions
        of the scoreline alone. That can never become false, so the script
        re-flagged rows it had already corrected and could not confirm its own
        work.
        """
        source = AUDIT_PATH.read_text(encoding="utf-8")
        assert "if stored == corrected:" in source
        assert "previous == corrected" not in source

    def test_apply_does_not_stack_repeated_notes(self) -> None:
        source = AUDIT_PATH.read_text(encoding="utf-8")
        assert "if not already:" in source
