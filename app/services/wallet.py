"""Wallet ledger.

``wallet_transactions`` is the financial source of truth. It is append-only:
nothing is ever updated or deleted. A reservation is resolved by writing a new
row that points back at it via ``parent_transaction_id``.

``users.credits`` is a cached balance, maintained in the same transaction as the
ledger write and reconcilable from the ledger at any time via :meth:`balance`.

Credits are held at reservation time and returned on failure. They are never
permanently consumed before the operation succeeds.

Concurrency is handled by locking the user row with ``SELECT ... FOR UPDATE``
before reading the balance. Without it, two concurrent scans both read the same
balance and both pass the sufficiency check — a double-spend. SQLite ignores
row locking, so the concurrency tests are marked ``integration`` and run against
PostgreSQL.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.enums import TransactionState, TransactionType
from app.database.models import User, WalletTransaction

logger = get_logger(__name__)


class WalletError(Exception):
    """Base class for wallet failures."""


class UnknownUserError(WalletError):
    """Raised when the user does not exist."""


class InsufficientCreditsError(WalletError):
    """Raised when the balance cannot cover a reservation."""

    def __init__(self, required: int, available: int) -> None:
        super().__init__(f"Requires {required} credits; balance is {available}.")
        self.required = required
        self.available = available


class InvalidAmountError(WalletError):
    """Raised when a non-positive amount is supplied."""


class ReservationAlreadyResolvedError(WalletError):
    """Raised when settling or releasing a reservation twice."""


class NotAReservationError(WalletError):
    """Raised when resolving a transaction that is not a reservation."""


class WalletLedger:
    """Append-only credit ledger operations for one database session.

    The caller owns the transaction boundary. Every method here writes within
    the caller's transaction so that a credit movement and the work it pays for
    commit or roll back together.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_user(self, user_id: int) -> User:
        """Load and lock the user row for the remainder of the transaction."""
        result = await self._session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise UnknownUserError(f"No user with id {user_id}.")
        return user

    async def _find_by_idempotency_key(
        self, user_id: int, idempotency_key: str
    ) -> WalletTransaction | None:
        """Return a previously recorded transaction for this key, if any."""
        result = await self._session.execute(
            select(WalletTransaction).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def _append(
        self,
        user: User,
        amount: int,
        transaction_type: TransactionType,
        state: TransactionState,
        description: str | None = None,
        reference: str | None = None,
        idempotency_key: str | None = None,
        parent_transaction_id: int | None = None,
    ) -> WalletTransaction:
        """Write one ledger row and update the cached balance.

        Args:
            user: The locked user row.
            amount: Signed credit movement. Negative debits.
        """
        balance_before = user.credits
        balance_after = balance_before + amount

        entry = WalletTransaction(
            user_id=user.id,
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            transaction_type=transaction_type,
            state=state,
            description=description,
            reference=reference,
            idempotency_key=idempotency_key,
            parent_transaction_id=parent_transaction_id,
        )
        self._session.add(entry)
        user.credits = balance_after
        await self._session.flush()

        logger.info(
            "wallet.entry_written",
            user_id=user.id,
            amount=amount,
            transaction_type=str(transaction_type),
            state=str(state),
            balance_after=balance_after,
        )
        return entry

    async def credit(
        self,
        user_id: int,
        amount: int,
        transaction_type: TransactionType = TransactionType.PURCHASE,
        description: str | None = None,
        reference: str | None = None,
        idempotency_key: str | None = None,
    ) -> WalletTransaction:
        """Add credits to a wallet.

        Raises:
            InvalidAmountError: If ``amount`` is not positive.
        """
        if amount <= 0:
            raise InvalidAmountError("Credit amount must be positive.")

        user = await self._lock_user(user_id)

        if idempotency_key:
            existing = await self._find_by_idempotency_key(user_id, idempotency_key)
            if existing is not None:
                logger.info("wallet.idempotent_replay", user_id=user_id)
                return existing

        return await self._append(
            user,
            amount=amount,
            transaction_type=transaction_type,
            state=TransactionState.COMPLETED,
            description=description,
            reference=reference,
            idempotency_key=idempotency_key,
        )

    async def reserve(
        self,
        user_id: int,
        amount: int,
        transaction_type: TransactionType = TransactionType.SCAN_RESERVATION,
        description: str | None = None,
        reference: str | None = None,
        idempotency_key: str | None = None,
    ) -> WalletTransaction:
        """Hold credits against a pending operation.

        The credits leave the available balance immediately but are returned in
        full by :meth:`release` if the operation fails.

        Raises:
            InvalidAmountError: If ``amount`` is not positive.
            InsufficientCreditsError: If the balance is too low.
        """
        if amount <= 0:
            raise InvalidAmountError("Reservation amount must be positive.")

        user = await self._lock_user(user_id)

        if idempotency_key:
            existing = await self._find_by_idempotency_key(user_id, idempotency_key)
            if existing is not None:
                logger.info("wallet.idempotent_replay", user_id=user_id)
                return existing

        if user.credits < amount:
            raise InsufficientCreditsError(required=amount, available=user.credits)

        return await self._append(
            user,
            amount=-amount,
            transaction_type=transaction_type,
            state=TransactionState.RESERVED,
            description=description,
            reference=reference,
            idempotency_key=idempotency_key,
        )

    async def _load_open_reservation(self, reservation_id: int) -> WalletTransaction:
        """Return the reservation, or raise if it is missing or already resolved."""
        reservation = await self._session.get(WalletTransaction, reservation_id)
        if reservation is None:
            raise WalletError(f"No transaction with id {reservation_id}.")
        if reservation.state is not TransactionState.RESERVED:
            raise NotAReservationError(
                f"Transaction {reservation_id} is {reservation.state}, not a reservation."
            )

        result = await self._session.execute(
            select(WalletTransaction.id).where(
                WalletTransaction.parent_transaction_id == reservation_id
            )
        )
        if result.first() is not None:
            raise ReservationAlreadyResolvedError(
                f"Reservation {reservation_id} has already been resolved."
            )
        return reservation

    async def settle(
        self, reservation_id: int, description: str | None = None
    ) -> WalletTransaction:
        """Finalise a reservation after the operation succeeded.

        The credits were already withheld at reservation time, so settlement
        writes a zero-amount marker rather than moving credits again. The marker
        exists so the ledger records the outcome of every reservation.

        Raises:
            ReservationAlreadyResolvedError: If already settled or released.
            NotAReservationError: If the transaction is not an open reservation.
        """
        reservation = await self._load_open_reservation(reservation_id)
        user = await self._lock_user(reservation.user_id)

        return await self._append(
            user,
            amount=0,
            transaction_type=TransactionType.SCAN_SETTLEMENT,
            state=TransactionState.COMPLETED,
            description=description,
            reference=reservation.reference,
            parent_transaction_id=reservation.id,
        )

    async def release(
        self, reservation_id: int, description: str | None = None
    ) -> WalletTransaction:
        """Return reserved credits after the operation failed.

        Raises:
            ReservationAlreadyResolvedError: If already settled or released.
            NotAReservationError: If the transaction is not an open reservation.
        """
        reservation = await self._load_open_reservation(reservation_id)
        user = await self._lock_user(reservation.user_id)

        return await self._append(
            user,
            amount=-reservation.amount,
            transaction_type=TransactionType.SCAN_RELEASE,
            state=TransactionState.RELEASED,
            description=description,
            reference=reservation.reference,
            parent_transaction_id=reservation.id,
        )

    async def balance(self, user_id: int) -> int:
        """Return the balance computed from the ledger, ignoring the cache."""
        result = await self._session.execute(
            select(WalletTransaction.amount).where(WalletTransaction.user_id == user_id)
        )
        return sum(result.scalars().all())

    async def reconcile(self, user_id: int) -> tuple[int, int, bool]:
        """Compare the cached balance against the ledger.

        Returns:
            ``(cached, ledger, matches)``.
        """
        user = await self._session.get(User, user_id)
        if user is None:
            raise UnknownUserError(f"No user with id {user_id}.")
        ledger_balance = await self.balance(user_id)
        matches = user.credits == ledger_balance
        if not matches:
            logger.error(
                "wallet.reconciliation_mismatch",
                user_id=user_id,
                cached=user.credits,
                ledger=ledger_balance,
            )
        return user.credits, ledger_balance, matches
