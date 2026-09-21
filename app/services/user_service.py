"""User registration and terms acceptance.

Acceptance state is derived from stored data rather than held in a session, so
it survives restarts and cannot be bypassed by a client that skips the
onboarding flow.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.terms import TERMS_VERSION
from app.database.models import User

logger = get_logger(__name__)


def has_valid_acceptance(user: User, required_version: str = TERMS_VERSION) -> bool:
    """Return whether a user may use the bot.

    Requires all three: an age confirmation, a terms acceptance, and an
    accepted version matching the current one. A user who accepted version 1.0
    is blocked once the current version becomes 1.1, and is asked to re-accept.

    Args:
        user: The user to check.
        required_version: Version that must have been accepted.
    """
    return (
        user.age_confirmed_at is not None
        and user.terms_accepted_at is not None
        and user.accepted_terms_version == required_version
    )


class UserService:
    """Registration and acceptance operations for one database session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Return the user for a Telegram ID, or ``None``."""
        result = await self._session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def register_or_touch(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        language_code: str | None = None,
    ) -> tuple[User, bool]:
        """Return the user for this Telegram ID, creating them if new.

        Registration deliberately does NOT grant credits or record any
        acceptance. A row exists so the gate has something to record against;
        it confers no access.

        Args:
            telegram_id: Telegram's numeric user ID.
            username: Telegram handle, if the user has one.
            first_name: Display name.
            language_code: Client language, for future localisation.

        Returns:
            ``(user, created)``.
        """
        user = await self.get_by_telegram_id(telegram_id)
        created = False

        # Admin status is config-driven: a user is admin iff their Telegram ID
        # is in TELEGRAM__ADMIN_IDS. Re-derived on every touch so the flag
        # always reflects the current config — granting when an ID is added,
        # revoking when it is removed — and never drifts from it.
        is_admin = telegram_id in set(get_settings().telegram.admin_ids)

        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                language_code=language_code,
                credits=0,
                is_admin=is_admin,
            )
            self._session.add(user)
            created = True
            logger.info("user.registered", telegram_id=telegram_id, is_admin=is_admin)
        else:
            # Telegram handles and names change; keep them current.
            user.username = username
            user.first_name = first_name
            user.language_code = language_code
            user.is_admin = is_admin

        user.last_seen_at = datetime.now(UTC)
        await self._session.flush()
        return user, created

    async def record_acceptance(self, user: User, version: str = TERMS_VERSION) -> User:
        """Record an explicit age and terms confirmation.

        Args:
            user: The confirming user.
            version: Terms version being accepted.
        """
        now = datetime.now(UTC)
        user.age_confirmed_at = now
        user.terms_accepted_at = now
        user.accepted_terms_version = version
        await self._session.flush()

        logger.info(
            "user.accepted_terms",
            telegram_id=user.telegram_id,
            terms_version=version,
        )
        return user
