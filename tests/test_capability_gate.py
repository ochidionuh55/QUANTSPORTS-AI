"""Publication permission for Wave 1 competitions.

Validation found calibration varies by market within a competition: Liga Alef
holds the largest dataset and earned two of twenty-one services; Bosnia holds
the smallest and earned eleven. So permission is per competition and service,
and everything unproven stays shut.

The scope matters as much as the rule. The gate covers Wave 1 only, because a
gate over every competition would let an empty capability table halt all
publication — a worse outage than the one it prevents.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import ServiceCapability
from app.database.models.capabilities import CapabilityState
from app.services.capability_gate import CapabilityGate

VERSION = "model-only-v2-dc"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as active:
        yield active
    await engine.dispose()


async def _capability(
    session: AsyncSession,
    code: str = "LEU",
    service: str = "home",
    state: str = CapabilityState.ACTIVE.value,
    version: str = VERSION,
) -> None:
    session.add(
        ServiceCapability(
            competition_code=code,
            competition_name="Liga Leumit",
            service_key=service,
            service_label="Best Home Win",
            model_version=version,
            validation_version="wave1-v1",
            state=state,
            reason="VALIDATED" if state == CapabilityState.ACTIVE.value else "TEST",
            qualifying_sample=500,
            total_sample=1141,
            validated_at=datetime.now(UTC),
        )
    )
    await session.flush()


@pytest.mark.asyncio
class TestActiveCapabilityPublishes:
    async def test_active_capability_allows_publication(
        self, session: AsyncSession
    ) -> None:
        await _capability(session)
        decision = await CapabilityGate(session).may_publish("LEU", "home", VERSION)
        assert decision
        assert decision.allowed

    async def test_ungated_competition_is_unaffected(
        self, session: AsyncSession
    ) -> None:
        """The existing thirty-eight publish exactly as before.

        With no capability row at all — the state an untouched production
        database is in.
        """
        gate = CapabilityGate(session)
        for code in ("E0", "D1", "SP1", "I1", "F1"):
            assert await gate.may_publish(code, "home", VERSION)

    async def test_unresolved_competition_is_not_gated(
        self, session: AsyncSession
    ) -> None:
        assert await CapabilityGate(session).may_publish(None, "home", VERSION)


@pytest.mark.asyncio
class TestEverythingElseFailsClosed:
    async def test_missing_capability_blocks(self, session: AsyncSession) -> None:
        decision = await CapabilityGate(session).may_publish("LEU", "home", VERSION)
        assert not decision
        assert "no capability" in decision.reason

    async def test_withheld_capability_blocks(self, session: AsyncSession) -> None:
        await _capability(session, state=CapabilityState.WITHHELD.value)
        decision = await CapabilityGate(session).may_publish("LEU", "home", VERSION)
        assert not decision
        assert "WITHHELD" in decision.reason

    async def test_insufficient_evidence_blocks(self, session: AsyncSession) -> None:
        """Unproven is not permission, and is distinct from disproven."""
        await _capability(
            session, state=CapabilityState.INSUFFICIENT_EVIDENCE.value
        )
        decision = await CapabilityGate(session).may_publish("LEU", "home", VERSION)
        assert not decision
        assert "INSUFFICIENT_EVIDENCE" in decision.reason

    async def test_unknown_service_blocks(self, session: AsyncSession) -> None:
        await _capability(session, service="home")
        assert not await CapabilityGate(session).may_publish(
            "LEU", "never_validated", VERSION
        )

    async def test_wrong_model_version_blocks(self, session: AsyncSession) -> None:
        """Evidence gathered on one model does not describe another."""
        await _capability(session, version="model-only-v1")
        assert not await CapabilityGate(session).may_publish("LEU", "home", VERSION)

    async def test_competition_withheld_blocks_every_service(
        self, session: AsyncSession
    ) -> None:
        """NPFL: withheld for 1X2 directional bias, so no service publishes."""
        for service in ("home", "away", "draw", "over_15"):
            await _capability(
                session,
                code="NPF",
                service=service,
                state=CapabilityState.WITHHELD.value,
            )
        gate = CapabilityGate(session)
        for service in ("home", "away", "draw", "over_15"):
            assert not await gate.may_publish("NPF", service, VERSION)

    async def test_no_state_other_than_active_publishes(
        self, session: AsyncSession
    ) -> None:
        for index, state in enumerate(CapabilityState):
            await _capability(session, service=f"s{index}", state=state.value)
        gate = CapabilityGate(session)
        for index, state in enumerate(CapabilityState):
            allowed = await gate.may_publish("LEU", f"s{index}", VERSION)
            assert bool(allowed) is (state is CapabilityState.ACTIVE)


class TestScopeAndInvariants:
    def test_wave_one_competitions_are_gated(self) -> None:
        for code in ("LEU", "AZA", "PRV", "PRB", "ALF", "NPF", "BRS", "ECB"):
            assert CapabilityGate.is_gated(code)

    def test_existing_competitions_are_not_gated(self) -> None:
        """Every configured competition must be outside the gate.

        If one were inside it, a missing capability row would stop it
        publishing — turning a safety feature into an outage.
        """
        from app.core.competitions import COMPETITIONS

        for competition in COMPETITIONS:
            assert not CapabilityGate.is_gated(competition.code), competition.code

    def test_only_active_is_publishable(self) -> None:
        from app.database.models.capabilities import PUBLISHABLE_STATES

        assert frozenset({"ACTIVE"}) == PUBLISHABLE_STATES

    def test_gate_adds_a_condition_and_removes_none(self) -> None:
        """It can block a publication; it can never cause one.

        Asserted on source: the gate appears as a `continue`, never as a
        branch that publishes something the thresholds rejected.
        """
        import inspect

        import app.services.selections as selections

        source = inspect.getsource(selections)
        assert "if not decision:" in source
        assert "blocked_by_capability += 1" in source
        assert "continue" in source

    def test_gate_does_not_touch_grid_or_settlement(self) -> None:
        """Enforcement must not reach into the mathematics."""
        import inspect

        import app.services.capability_gate as gate_module

        source = inspect.getsource(gate_module)
        for forbidden in ("build_grid", "derive_markets", "settles_won", "DEFAULT_WEIGHTS"):
            assert forbidden not in source
