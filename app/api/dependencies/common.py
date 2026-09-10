"""Shared FastAPI dependencies.

Infrastructure clients live on ``app.state`` so that tests can substitute
fakes by overriding these dependencies rather than patching module globals.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.infrastructure.database import Database
from app.infrastructure.redis import RedisClient


def get_settings_dep(request: Request) -> Settings:
    """Return the settings object attached to the running application."""
    return request.app.state.settings  # type: ignore[no-any-return]


def get_database(request: Request) -> Database:
    """Return the process database handle."""
    return request.app.state.database  # type: ignore[no-any-return]


def get_redis(request: Request) -> RedisClient:
    """Return the process Redis client."""
    return request.app.state.redis  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RedisDep = Annotated[RedisClient, Depends(get_redis)]
