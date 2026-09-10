# syntax=docker/dockerfile:1.7
# One image, three roles. The API, bot and worker differ only by command, so
# they share a build cache and can never drift out of version sync.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*


# --- Dependency layer -------------------------------------------------------
FROM base AS deps

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt


# --- Development image ------------------------------------------------------
FROM deps AS development

RUN pip install --no-cache-dir -r requirements-dev.txt

RUN useradd --create-home --uid 1000 quantsport
COPY --chown=quantsport:quantsport . .
USER quantsport

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# --- Production image -------------------------------------------------------
FROM deps AS production

ARG BUILD_SHA=unknown
ENV BUILD_SHA=${BUILD_SHA}

RUN useradd --create-home --uid 1000 quantsport

# Everything the running services need, and nothing else. Tests and dev
# tooling are deliberately absent.
#
# The migrations and alembic.ini are not optional: the production stack runs
# `alembic upgrade head` in its own container before the app starts, and
# without them that container fails and nothing downstream comes up.
COPY --chown=quantsport:quantsport app ./app
COPY --chown=quantsport:quantsport migrations ./migrations
COPY --chown=quantsport:quantsport scripts ./scripts
COPY --chown=quantsport:quantsport data ./data
COPY --chown=quantsport:quantsport alembic.ini pyproject.toml ./

USER quantsport

# Overridden per service in docker-compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
