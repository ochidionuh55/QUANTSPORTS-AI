"""Wallet ledger tests.

Runs against in-memory SQLite. Note that SQLite ignores ``FOR UPDATE``, so
these tests verify ledger *semantics* — append-only behaviour, balance
arithmetic, idempotent replay, double-resolution guards — but not locking.
True concurrency behaviour is exercised by the integration tests against
PostgreSQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.enums import TransactionState, TransactionType
from app.database.models import User, WalletTransaction
from app.services.wallet import (
    InsufficientCreditsError,
    InvalidAmountError,
    NotAReservationError,
    ReservationAlreadyResolvedError,
    UnknownUserError,
    WalletLedger,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Provide a session against a fresh in-memory schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as active:
        yield active
    await engine.dispose()


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    """Create a user holding 100 credits.

    The opening balance is granted *through the ledger*, not by setting
    ``credits`` directly. Any credit that reaches a wallet without a matching
    ledger entry is unreconcilable by construction — see
    ``test_direct_credit_assignment_breaks_reconciliation``.
    """
    record = User(telegram_id=42, username="tester", first_name="Test", credits=0)
    session.add(record)
    await session.flush()
    await WalletLedger(session).credit(
        record.id,
        100,
        transaction_type=TransactionType.SIGNUP_BONUS,
        description="Opening balance",
    )
    return record


@pytest_asyncio.fixture
async def ledger(session: AsyncSession) -> WalletLedger:
    """Return a ledger bound to the test session."""
    return WalletLedger(session)


class TestReservation:
    """Credits are held, not consumed, until the operation resolves."""

    async def test_reserve_reduces_balance(self, ledger: WalletLedger, user: User) -> None:
        entry = await ledger.reserve(user.id, 30)
        assert entry.amount == -30
        assert entry.state is TransactionState.RESERVED
        assert entry.balance_before == 100
        assert entry.balance_after == 70
        assert user.credits == 70

    async def test_reserve_rejects_insufficient_balance(
        self, ledger: WalletLedger, user: User
    ) -> None:
        with pytest.raises(InsufficientCreditsError) as info:
            await ledger.reserve(user.id, 500)
        assert info.value.required == 500
        assert info.value.available == 100
        assert user.credits == 100

    async def test_reserve_rejects_non_positive(self, ledger: WalletLedger, user: User) -> None:
        for amount in (0, -5):
            with pytest.raises(InvalidAmountError):
                await ledger.reserve(user.id, amount)

    async def test_reserve_exact_balance_is_allowed(self, ledger: WalletLedger, user: User) -> None:
        await ledger.reserve(user.id, 100)
        assert user.credits == 0

    async def test_unknown_user(self, ledger: WalletLedger) -> None:
        with pytest.raises(UnknownUserError):
            await ledger.reserve(999_999, 1)


class TestSettlement:
    """Settlement confirms a spend; release returns it in full."""

    async def test_settle_does_not_move_credits_again(
        self, ledger: WalletLedger, user: User
    ) -> None:
        reservation = await ledger.reserve(user.id, 30)
        settlement = await ledger.settle(reservation.id)

        assert settlement.amount == 0
        assert settlement.parent_transaction_id == reservation.id
        assert settlement.state is TransactionState.COMPLETED
        assert user.credits == 70

    async def test_release_returns_credits(self, ledger: WalletLedger, user: User) -> None:
        reservation = await ledger.reserve(user.id, 30)
        release = await ledger.release(reservation.id)

        assert release.amount == 30
        assert release.state is TransactionState.RELEASED
        assert user.credits == 100

    async def test_cannot_settle_twice(self, ledger: WalletLedger, user: User) -> None:
        reservation = await ledger.reserve(user.id, 30)
        await ledger.settle(reservation.id)
        with pytest.raises(ReservationAlreadyResolvedError):
            await ledger.settle(reservation.id)

    async def test_cannot_release_after_settling(self, ledger: WalletLedger, user: User) -> None:
        reservation = await ledger.reserve(user.id, 30)
        await ledger.settle(reservation.id)
        with pytest.raises(ReservationAlreadyResolvedError):
            await ledger.release(reservation.id)

    async def test_cannot_resolve_a_non_reservation(self, ledger: WalletLedger, user: User) -> None:
        topup = await ledger.credit(user.id, 50)
        with pytest.raises(NotAReservationError):
            await ledger.settle(topup.id)


class TestIdempotency:
    """A replayed request must return the original result, not repeat it."""

    async def test_repeated_reservation_is_not_double_charged(
        self, ledger: WalletLedger, user: User
    ) -> None:
        first = await ledger.reserve(user.id, 30, idempotency_key="scan-abc")
        second = await ledger.reserve(user.id, 30, idempotency_key="scan-abc")

        assert first.id == second.id
        assert user.credits == 70

    async def test_repeated_credit_is_not_double_applied(
        self, ledger: WalletLedger, user: User
    ) -> None:
        await ledger.credit(user.id, 50, idempotency_key="payment-xyz")
        await ledger.credit(user.id, 50, idempotency_key="payment-xyz")
        assert user.credits == 150

    async def test_distinct_keys_apply_separately(self, ledger: WalletLedger, user: User) -> None:
        await ledger.reserve(user.id, 10, idempotency_key="scan-1")
        await ledger.reserve(user.id, 10, idempotency_key="scan-2")
        assert user.credits == 80


class TestAppendOnlyLedger:
    """The ledger is the source of truth and must reconcile with the cache."""

    async def test_balance_matches_cache_through_a_full_cycle(
        self, ledger: WalletLedger, user: User, session: AsyncSession
    ) -> None:
        await ledger.credit(user.id, 50, transaction_type=TransactionType.PURCHASE)
        first = await ledger.reserve(user.id, 40)
        await ledger.settle(first.id)
        second = await ledger.reserve(user.id, 25)
        await ledger.release(second.id)

        cached, from_ledger, matches = await ledger.reconcile(user.id)
        assert matches
        assert cached == from_ledger == 110

    async def test_nothing_is_mutated(
        self, ledger: WalletLedger, user: User, session: AsyncSession
    ) -> None:
        """A resolved reservation keeps its original row untouched."""
        reservation = await ledger.reserve(user.id, 30)
        await ledger.release(reservation.id)

        stored = await session.get(WalletTransaction, reservation.id)
        assert stored is not None
        assert stored.state is TransactionState.RESERVED
        assert stored.amount == -30

    async def test_every_entry_records_surrounding_balances(
        self, ledger: WalletLedger, user: User
    ) -> None:
        entries = [
            await ledger.credit(user.id, 20),
            await ledger.reserve(user.id, 60),
        ]
        for entry in entries:
            assert entry.balance_after == entry.balance_before + entry.amount

    async def test_direct_credit_assignment_breaks_reconciliation(
        self, ledger: WalletLedger, user: User, session: AsyncSession
    ) -> None:
        """Credits granted outside the ledger must be detectable.

        This is the failure mode reconciliation exists to catch: if any code
        path ever sets ``users.credits`` directly, the cache silently diverges
        from the ledger. Reconciliation must report that rather than trust the
        cache.
        """
        user.credits += 500
        await session.flush()

        cached, from_ledger, matches = await ledger.reconcile(user.id)
        assert not matches
        assert cached == 600
        assert from_ledger == 100
