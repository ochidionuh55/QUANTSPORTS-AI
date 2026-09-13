"""User accounts and the wallet ledger."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, CreatedAtMixin, IntPrimaryKeyMixin, TimestampMixin, enum_column
from app.database.enums import TransactionState, TransactionType, UserPlan

if TYPE_CHECKING:
    from app.database.models.scan import Scan


class User(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A Telegram user.

    ``credits`` is a cached balance for fast reads. The authoritative balance
    is the sum of completed entries in ``wallet_transactions``; the two must
    reconcile, and the ledger wins any disagreement.
    """

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))

    credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    subscription_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, default="free", server_default=text("'free'")
    )
    """``free``, ``trial`` or ``pro``.

    Expiry is not stored as a state. It is computed from
    ``subscription_expires_at`` on every check, so a lapsed subscription cannot
    keep working because a background job failed to run.
    """

    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscription_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Whether a trial has ever been started.

    Kept apart from the tier so a trial cannot be restarted simply by letting
    it lapse.
    """

    plan: Mapped[UserPlan] = mapped_column(
        enum_column(UserPlan, "user_plan"),
        default=UserPlan.FREE,
        server_default=UserPlan.FREE.value,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Compliance: recorded at registration, not inferred later.
    age_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Which version of the terms was accepted. Stored rather than assumed so
    # that a material change to the terms can require re-acceptance without
    # re-prompting everyone for a cosmetic edit.
    accepted_terms_version: Mapped[str | None] = mapped_column(String(16))

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    transactions: Mapped[list[WalletTransaction]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    scans: Mapped[list[Scan]] = relationship(back_populates="user")

    __table_args__ = (
        CheckConstraint("credits >= 0", name="credits_non_negative"),
        Index("ix_users_last_seen_at", "last_seen_at"),
    )


class WalletTransaction(IntPrimaryKeyMixin, CreatedAtMixin, Base):
    """An append-only ledger entry.

    Rows are never updated in place. A reservation that is later settled or
    released produces a *new* row referencing the original through
    ``parent_transaction_id``, so the full history stays auditable.

    ``balance_before`` and ``balance_after`` are recorded on every row so that
    a divergence between the ledger and ``users.credits`` can be traced to the
    exact entry that caused it.
    """

    __tablename__ = "wallet_transactions"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    """Signed: negative debits the user, positive credits them."""

    balance_before: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)

    transaction_type: Mapped[TransactionType] = mapped_column(
        enum_column(TransactionType, "transaction_type"), nullable=False
    )
    state: Mapped[TransactionState] = mapped_column(
        enum_column(TransactionState, "transaction_state"), nullable=False
    )

    parent_transaction_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("wallet_transactions.id", ondelete="RESTRICT")
    )

    description: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(128))

    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    """Replaying a request with the same key returns the original result."""

    user: Mapped[User] = relationship(back_populates="transactions")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_wallet_transactions_user_id_idempotency_key"
        ),
        CheckConstraint("balance_after >= 0", name="balance_after_non_negative"),
        CheckConstraint(
            "balance_after = balance_before + amount", name="balance_arithmetic_consistent"
        ),
        Index("ix_wallet_transactions_user_id_created_at", "user_id", "created_at"),
        Index("ix_wallet_transactions_state", "state"),
    )
