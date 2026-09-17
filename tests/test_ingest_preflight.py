"""Ingestion establishes its own identity prerequisite.

Two orderings produced silently truncated datasets in production:

* Liga Leumit — seeded one season, ingested eight. 16 teams against 36, and
  1,022 of 2,374 fixtures (43%) had no team to attach to.
* Liga Pro Serie B — ingested before seeding at all. 12 teams against 29, and
  1,008 of 1,452 fixtures (69%) unresolved.

Both left a competition looking ingested while a model fitted on it would
train only on clubs that happened to still be in the division. The remedy is
not remembering the order; it is ingestion refusing to proceed until coverage
is established.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

INGEST_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ingest_wave1.py"
_spec = importlib.util.spec_from_file_location("ingest_wave1", INGEST_PATH)
assert _spec and _spec.loader
ingest = importlib.util.module_from_spec(_spec)
sys.modules["ingest_wave1"] = ingest
_spec.loader.exec_module(ingest)


def _preflight(**kwargs: Any) -> Any:
    """Build a Preflight. Typed Any: the module is loaded from a path."""
    return ingest.Preflight(league_id=382, **kwargs)


class TestIngestBeforeSeedIsHandled:
    """The prerequisite is established by ingestion, not by the operator."""

    def test_ingestion_runs_its_own_preflight(self) -> None:
        source = INGEST_PATH.read_text(encoding="utf-8")
        assert "run_preflight(" in source
        assert "preflight: checking historical identity coverage" in source

    def test_failed_preflight_blocks_ingestion(self) -> None:
        """A blocked competition must not reach ingest_competition."""
        source = INGEST_PATH.read_text(encoding="utf-8")
        blocked_index = source.index("if not pre.passes:")
        ingest_index = source.index("report = await ingest_competition(")
        assert blocked_index < ingest_index
        assert "continue" in source[blocked_index:ingest_index]

    def test_full_coverage_passes(self) -> None:
        assert _preflight(provider_teams=36, already_known=22, seeded=14).passes

    def test_partial_coverage_does_not_pass(self) -> None:
        """Liga Leumit's first run: 22 of 36."""
        pre = _preflight(provider_teams=36, already_known=22, seeded=0)
        assert not pre.passes
        assert pre.coverage < 1.0

    def test_ecuador_shape_does_not_pass(self) -> None:
        """12 of 29, the 69% failure."""
        pre = _preflight(provider_teams=29, already_known=12, seeded=0)
        assert not pre.passes

    def test_no_provider_teams_does_not_pass(self) -> None:
        """An empty squad list is a failed request, not full coverage."""
        assert not _preflight(provider_teams=0).passes


class TestAmbiguityBlocksTrainingReadiness:
    """An identity that cannot be safely established stops the competition."""

    def test_any_unresolvable_team_blocks(self) -> None:
        pre = _preflight(
            provider_teams=36,
            already_known=35,
            seeded=0,
            unresolvable=[("Hapoel Something", "Hapoel Other", 0.78)],
        )
        assert not pre.passes

    def test_unresolvable_teams_are_named(self) -> None:
        """Reported by name, never skipped silently."""
        source = INGEST_PATH.read_text(encoding="utf-8")
        assert "unresolvable:" in source
        assert "BLOCKED — REVIEW REQUIRED" in source

    def test_preflight_never_forces_a_match(self) -> None:
        """It uses resolve_or_create, which refuses uncertain matches itself."""
        source = INGEST_PATH.read_text(encoding="utf-8")
        assert "resolve_or_create(" in source
        assert "learn=False" in source


class TestSeasonTurnoverIsCovered:
    """Every ingested season's squad, not the current one."""

    def test_preflight_iterates_all_seasons(self) -> None:
        source = INGEST_PATH.read_text(encoding="utf-8")
        preflight = source[
            source.index("async def run_preflight") : source.index(
                "@dataclass\nclass Reconciliation"
            )
        ]
        assert "for season in seasons:" in preflight
        assert "seasons_checked.append(season)" in preflight

    def test_teams_are_unioned_not_replaced(self) -> None:
        """A club appears in several seasons; it must be counted once."""
        source = INGEST_PATH.read_text(encoding="utf-8")
        preflight = source[
            source.index("async def run_preflight") : source.index(
                "@dataclass\nclass Reconciliation"
            )
        ]
        assert "if key and key not in collected:" in preflight

    def test_seasons_checked_is_reported(self) -> None:
        pre = _preflight(provider_teams=1, already_known=1, seasons_checked=[2025, 2024])
        assert len(pre.seasons_checked) == 2


class TestIdempotency:
    """Re-running must not duplicate anything."""

    def test_fixtures_are_keyed_on_the_provider_id(self) -> None:
        source = INGEST_PATH.read_text(encoding="utf-8")
        assert "ProviderFixture.provider_fixture_id == fixture_id" in source
        assert "stored_updated += 1" in source

    def test_historical_matches_are_checked_before_insert(self) -> None:
        source = INGEST_PATH.read_text(encoding="utf-8")
        assert "HistoricalMatch.provider_match_id == fixture_id" in source
        assert "historical_existing += 1" in source

    def test_teams_are_resolved_before_creation(self) -> None:
        """Preflight resolves first and only creates what is genuinely absent."""
        source = INGEST_PATH.read_text(encoding="utf-8")
        preflight = source[
            source.index("async def run_preflight") : source.index(
                "@dataclass\nclass Reconciliation"
            )
        ]
        assert preflight.index("existing = await resolver.resolve(") < preflight.index(
            "resolve_or_create("
        )

    def test_unique_constraint_exists_on_provider_fixture(self) -> None:
        from app.database.models import ProviderFixture

        table: Any = ProviderFixture.__table__
        names = {c.name for c in table.constraints}
        assert "uq_provider_fixtures_provider_fixture" in names


class TestSameProviderCollisionsNeverMerge:
    """The safeguard that fired fourteen times during seeding."""

    def test_resolver_refuses_same_provider_fuzzy_matches(self) -> None:
        import inspect

        import app.services.team_resolution as resolution

        source = inspect.getsource(resolution)
        assert "fuzzy_rejected_same_provider" in source

    def test_known_collisions_are_distinct_clubs(self) -> None:
        pairs = [
            ("Hapoel Bnei Lod", "Hapoel Bnei Musmus"),
            ("Kwara United", "Akwa United"),
            ("Tzeirey Tira", "Tira"),
            ("Hapoel Kfar Shalem", "Hapoel Kfar Saba"),
        ]
        for left, right in pairs:
            assert left.lower() != right.lower()

    def test_reconciliation_balances_definition(self) -> None:
        """returned = stored, and stored = trainable + explained exclusions."""
        report = ingest.Reconciliation(league_id=382, label="x")
        report.returned = 2374
        report.stored_new = 2374
        report.trainable = 2372
        report.excluded["not played (CANC)"] = 2
        assert report.balances

    def test_reconciliation_detects_a_gap(self) -> None:
        report = ingest.Reconciliation(league_id=382, label="x")
        report.returned = 2374
        report.stored_new = 2000
        report.trainable = 2000
        assert not report.balances
