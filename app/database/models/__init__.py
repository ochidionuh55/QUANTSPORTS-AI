"""ORM models.

Every model must be imported here. Alembic's autogenerate only sees tables
that are registered on ``Base.metadata``, so a model missing from this list is
silently omitted from migrations.
"""

from __future__ import annotations

from app.database.base import Base
from app.database.models.activity import (
    FixtureView,
    SavedFixture,
    UserPreferences,
)
from app.database.models.analysis import StoredAnalysis
from app.database.models.highlights import HighlightFollow, HighlightSelection
from app.database.models.historical import HistoricalMatch, ModelVersion
from app.database.models.ingestion import DatasetIngestion
from app.database.models.match import Market, Match, OddsSnapshot, Outcome
from app.database.models.scan import BookingCode, Prediction, Scan
from app.database.models.scan_telemetry import (
    RejectionCode,
    ScanDecision,
    ScanRun,
    ScanRunStatus,
    ScanStage,
)
from app.database.models.selections import (
    DailySnapshot,
    SelectionAudit,
    ServiceSelection,
)
from app.database.models.team import Competition, Team, TeamAlias
from app.database.models.tracking import SettledPrediction, UserFeedback
from app.database.models.user import User, WalletTransaction

__all__ = [
    "Base",
    "BookingCode",
    "Competition",
    "DailySnapshot",
    "DatasetIngestion",
    "FixtureView",
    "HighlightFollow",
    "HighlightSelection",
    "HistoricalMatch",
    "Market",
    "Match",
    "ModelVersion",
    "OddsSnapshot",
    "Outcome",
    "Prediction",
    "RejectionCode",
    "SavedFixture",
    "Scan",
    "ScanDecision",
    "ScanRun",
    "ScanRunStatus",
    "ScanStage",
    "SelectionAudit",
    "ServiceSelection",
    "SettledPrediction",
    "StoredAnalysis",
    "Team",
    "TeamAlias",
    "User",
    "UserFeedback",
    "UserPreferences",
    "WalletTransaction",
]

# Imported for its side effect: attaches covering indexes to the tables above.
from app.database.models import indexes  # noqa: F401
