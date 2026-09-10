"""Ingestion provenance.

One row per dataset ingested. Free historical sources revise files in place and
keep no vintage history, so what a source said six months ago is unrecoverable.
This table does not fix that — it makes *our* ingestion identifiable, which is
the part we control: given a prediction, we can name the dataset, the exact
bytes it came from, and the parser that read them.

Re-ingesting the same competition and season is expected. Each attempt is
recorded, so a change in ``content_hash`` between rows is visible evidence that
the upstream source was revised.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    Base,
    CreatedAtMixin,
    IntPrimaryKeyMixin,
    JSONType,
)


class DatasetIngestion(IntPrimaryKeyMixin, CreatedAtMixin, Base):
    """Provenance for one ingested competition-season."""

    __tablename__ = "dataset_ingestions"
    __table_args__ = (
        Index(
            "ix_dataset_ingestions_lookup",
            "competition_external_id",
            "season",
            "created_at",
        ),
        Index("ix_dataset_ingestions_content_hash", "content_hash"),
    )

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)

    competition_external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    season: Mapped[str] = mapped_column(String(16), nullable=False)

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256 of the raw source bytes. Also written to
    ``historical_matches.data_version`` so every match points back here."""

    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)

    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Already present from a previous run. Non-zero proves idempotency held."""

    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_team_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    teams_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aliases_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    rejections: Mapped[dict[str, int]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    """Counts by rejection reason. The full per-row detail lives in the returned
    report; persisting every bad row would grow without bound for no benefit."""

    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
