"""System metadata endpoints.

Exposes the current feature-flag state so that the model-promotion gate is
observable in every environment rather than buried in configuration.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies.common import SettingsDep
from app.core.version import APP_PHASE, APP_VERSION, build_sha

router = APIRouter(prefix="/system", tags=["system"])


class FeatureState(BaseModel):
    """Serialisable view of the runtime feature flags."""

    research_mode: bool
    value_detection_enabled: bool
    promoted_model_version: str | None
    booking_codes_enabled: bool
    payments_enabled: bool
    scheduled_odds_capture_enabled: bool


class SystemInfo(BaseModel):
    """Non-sensitive description of the running system."""

    app_name: str
    version: str
    phase: str
    build: str
    environment: str
    service: str
    features: FeatureState


@router.get("/info", summary="System and feature-flag state")
async def system_info(settings: SettingsDep) -> SystemInfo:
    """Return version, environment and feature-flag state.

    Contains no secrets and no connection strings.
    """
    return SystemInfo(
        app_name=settings.app_name,
        version=APP_VERSION,
        phase=APP_PHASE,
        build=build_sha(),
        environment=str(settings.environment),
        service=str(settings.service_role),
        features=FeatureState(**settings.features.model_dump()),
    )
