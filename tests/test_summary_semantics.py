"""What each headline number means, pinned so it cannot drift.

One field called ``competitions`` was trying to answer four different
questions. It was derived from ``CSV_COMPETITIONS`` — a name suggesting a
manifest of CSV files, but actually a projection of the scan universe, so
adding six Wave 1 competitions silently changed what the homepage claimed.

The questions are genuinely different and now have separate fields:

    corpus_competitions   competitions we hold verified history for
    scan_competitions     competitions analysed daily
    capability decisions  competition x service, validated separately

A competition can be researched and withheld at once. NPFL is exactly that:
its history is ingested and reconciled, and it publishes nothing.
"""

from __future__ import annotations

import inspect

import app.api.routes.public as public
from app.core.competitions import COMPETITIONS


class TestFieldsExist:
    def test_summary_exposes_all_three_competition_counts(self) -> None:
        fields = public.PlatformSummary.model_fields
        for name in ("competitions", "corpus_competitions", "scan_competitions"):
            assert name in fields

    def test_legacy_field_is_documented_as_legacy(self) -> None:
        """Its meaning must not change underneath an existing consumer."""
        description = public.PlatformSummary.model_fields["competitions"].description
        assert description is not None
        assert "legacy" in description.lower()

    def test_new_fields_state_what_they_measure(self) -> None:
        fields = public.PlatformSummary.model_fields
        corpus = fields["corpus_competitions"].description or ""
        scan = fields["scan_competitions"].description or ""
        assert "held" in corpus.lower()
        assert "not" in scan.lower() and "validation" in scan.lower()


class TestCorpusSemantics:
    """Data possession, not publication eligibility."""

    def test_corpus_counts_through_the_database_not_config(self) -> None:
        source = inspect.getsource(public.summary)
        assert "HistoricalMatch.competition_id" in source
        assert "distinct" in source

    def test_corpus_excludes_rows_with_no_competition(self) -> None:
        """The foreign key is ON DELETE SET NULL, so orphans exist.

        Counting them would report a competition that is no longer there.
        """
        source = inspect.getsource(public.summary)
        assert "HistoricalMatch.competition_id.is_not(None)" in source

    def test_corpus_is_not_filtered_by_scan_membership(self) -> None:
        """A withheld competition still contributes history it genuinely holds."""
        source = inspect.getsource(public.summary)
        corpus_block = source[
            source.index("corpus = await session.execute") : source.index(
                "corpus_competitions = int("
            )
        ]
        for leak in ("COMPETITIONS", "ServiceCapability", "ACTIVE", "GATED"):
            assert leak not in corpus_block


class TestScanSemantics:
    def test_scan_uses_the_canonical_universe(self) -> None:
        source = inspect.getsource(public.summary)
        assert "scan_competitions=len(COMPETITIONS)" in source

    def test_scan_is_the_live_universe_today(self) -> None:
        """Currently 44: the established thirty-eight plus six Wave 1."""
        assert len(COMPETITIONS) == 44

    def test_withheld_wave_one_competitions_are_not_scanned(self) -> None:
        """NPFL and Ecuador are researched, not published."""
        from app.core.competitions import code_for_api_id

        assert code_for_api_id(399) is None
        assert code_for_api_id(243) is None


class TestNumbersAreDerived:
    def test_matches_and_teams_come_from_the_database(self) -> None:
        """Neither may become a frozen marketing figure."""
        source = inspect.getsource(public.summary)
        assert "select(func.count()).select_from(HistoricalMatch)" in source
        assert "select(func.count()).select_from(Team)" in source

    def test_no_hard_coded_headline_integers(self) -> None:
        source = inspect.getsource(public.summary)
        for stale in ("113000", "113,029", "1021", "= 38", "= 44"):
            assert stale not in source
