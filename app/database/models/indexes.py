"""Covering indexes for foreign key columns.

PostgreSQL indexes the *referenced* side of a foreign key automatically, but
never the referencing side. Two consequences matter here:

1. Joins on these columns degrade to sequential scans as the tables grow, and
   ``predictions``, ``outcomes`` and ``odds_snapshots`` are the tables that grow
   fastest.
2. Deleting a parent row forces a full scan of every child table to enforce
   ``ON DELETE CASCADE`` or ``SET NULL``. Removing one stale match would scan
   all of ``predictions``.

These are declared here rather than scattered across ``__table_args__`` so the
set can be reviewed in one place against the query patterns each phase adds.
"""

from __future__ import annotations

from sqlalchemy import Index

from app.database.models.historical import HistoricalMatch
from app.database.models.match import Match
from app.database.models.scan import BookingCode, Prediction, Scan
from app.database.models.team import Team, TeamAlias
from app.database.models.user import WalletTransaction

# Hot paths: written on every scan, read on every prediction lookup.
Index("ix_predictions_outcome_id", Prediction.outcome_id)
Index("ix_predictions_tradeable_snapshot_id", Prediction.tradeable_snapshot_id)
Index("ix_predictions_reference_snapshot_id", Prediction.reference_snapshot_id)

# Team resolution walks aliases by team on every unresolved fixture.
Index("ix_team_aliases_team_id", TeamAlias.team_id)

# Fixture ingestion resolves both sides of every match.
Index("ix_matches_home_team_id", Match.home_team_id)
Index("ix_matches_away_team_id", Match.away_team_id)
Index("ix_matches_competition_id", Match.competition_id)

# Historical ingestion and Elo iterate by competition.
Index("ix_historical_matches_competition_id", HistoricalMatch.competition_id)
Index("ix_teams_competition_id", Team.competition_id)

# Ledger audit walks reservation chains parent-first.
Index("ix_wallet_transactions_parent_transaction_id", WalletTransaction.parent_transaction_id)

# Admin and analytics lookups.
Index("ix_booking_codes_scan_id", BookingCode.scan_id)
Index("ix_scans_model_version_id", Scan.model_version_id)
