"""Deployment configuration tests.

A production image missing a file it needs fails at deploy time, on a server,
usually at an inconvenient hour. These checks are cheap and catch it in CI
instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def dockerfile() -> str:
    """The Dockerfile source."""
    return (ROOT / "docker" / "Dockerfile").read_text()


@pytest.fixture(scope="module")
def production_stage(dockerfile: str) -> str:
    """Only the production stage, so development copies do not count."""
    marker = "FROM deps AS production"
    assert marker in dockerfile
    return dockerfile[dockerfile.index(marker) :]


@pytest.fixture(scope="module")
def prod_compose() -> dict:
    """The parsed production compose file."""
    return yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())


class TestProductionImage:
    """The image must contain everything the services need."""

    @pytest.mark.parametrize(
        "required",
        ["app", "migrations", "scripts", "alembic.ini"],
    )
    def test_required_paths_are_copied(self, production_stage: str, required: str) -> None:
        """Regression: the image shipped without alembic.ini or migrations.

        The migrate container ran `alembic upgrade head`, could not find its
        config, exited 255, and every downstream service refused to start.
        """
        assert required in production_stage, (
            f"'{required}' is not copied into the production image. "
            "The development stage copying everything hides this."
        )

    def test_runs_as_a_non_root_user(self, production_stage: str) -> None:
        assert "USER quantsport" in production_stage

    def test_tests_are_not_shipped(self, production_stage: str) -> None:
        """Test files have no place in a production image."""
        assert "COPY --chown=quantsport:quantsport tests" not in production_stage


class TestProductionCompose:
    """The stack must be able to start unattended."""

    def test_migrations_run_before_the_app(self, prod_compose: dict) -> None:
        """A deployment must never run new code against an old schema."""
        migrate = prod_compose["services"]["migrate"]
        assert migrate["command"] == ["alembic", "upgrade", "head"]
        assert migrate["restart"] == "no"

        for name in ("api", "bot", "worker"):
            depends = prod_compose["services"][name].get("depends_on", {})
            assert "migrate" in depends, f"{name} must wait for migrations"
            assert depends["migrate"]["condition"] == "service_completed_successfully"

    def test_services_restart_after_a_host_reboot(self, prod_compose: dict) -> None:
        """The whole point is running without anyone logged in."""
        for name in ("api", "bot", "worker", "postgres", "redis"):
            assert prod_compose["services"][name]["restart"] == "always"

    def test_database_is_not_exposed(self, prod_compose: dict) -> None:
        """An exposed database is how servers get taken over."""
        for name in ("postgres", "redis"):
            assert "ports" not in prod_compose["services"][name]

    def test_api_binds_to_loopback(self, prod_compose: dict) -> None:
        ports = prod_compose["services"]["api"].get("ports", [])
        assert ports
        assert all(str(p).startswith("127.0.0.1:") for p in ports)

    def test_logs_rotate(self, prod_compose: dict) -> None:
        """Without rotation a chatty worker fills the disk in weeks."""
        options = prod_compose["services"]["api"]["logging"]["options"]
        assert options["max-size"]
        assert options["max-file"]

    def test_data_persists(self, prod_compose: dict) -> None:
        assert "postgres-data" in prod_compose["volumes"]
        assert "redis-data" in prod_compose["volumes"]

    def test_a_backup_service_exists(self, prod_compose: dict) -> None:
        assert "backup" in prod_compose["services"]


class TestOperationalScripts:
    """The scripts referenced by the docs must exist and be runnable."""

    @pytest.mark.parametrize(
        "script",
        ["smoke_test.py", "ingest.py", "backfill.py", "backtest.py", "backup.sh"],
    )
    def test_script_exists(self, script: str) -> None:
        assert (ROOT / "scripts" / script).is_file()

    def test_deployment_guide_exists(self) -> None:
        guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
        assert "docker-compose.prod.yml" in guide
        assert "smoke_test.py" in guide

    def test_production_env_template_exists(self) -> None:
        """Production needs different settings; nobody should have to
        remember which."""
        template = (ROOT / ".env.production.example").read_text()
        assert "ENVIRONMENT=production" in template
        assert "DEBUG=false" in template
        assert "OBSERVABILITY__LOG_FORMAT=json" in template
        assert "FEATURES__VALUE_DETECTION_ENABLED=false" in template

    def test_production_template_ships_no_secrets(self) -> None:
        """A template with a real token in it will end up in a repository."""
        template = (ROOT / ".env.production.example").read_text()
        for line in template.splitlines():
            if line.startswith(("POSTGRES__PASSWORD", "TELEGRAM__BOT_TOKEN", "API_FOOTBALL_KEY")):
                assert line.endswith("="), f"{line} must be empty in the template"

    def test_backup_writes_atomically(self) -> None:
        """An interrupted dump must not replace a good backup."""
        script = (ROOT / "scripts" / "backup.sh").read_text()
        assert ".partial" in script
        assert "RETENTION_DAYS" in script


class TestDataDirectoryResolution:
    """The CSV directory sits in a different place per environment.

    Regression: the scripts defaulted to ``/data``, which exists only as a
    development bind mount. In production the data is baked into the image
    beside the application, so ingest failed with "No such directory: /data"
    on an image that contained the data all along.
    """

    def test_resolves_to_an_existing_directory(self) -> None:
        from app.core.paths import data_dir

        assert data_dir().is_dir()

    def test_environment_variable_wins(self, tmp_path, monkeypatch) -> None:
        from app.core.paths import DATA_DIR_ENV, data_dir

        monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
        assert data_dir() == tmp_path

    def test_candidates_are_reported_for_error_messages(self) -> None:
        """A failure must say where it looked, not just that it failed."""
        from app.core.paths import candidate_data_dirs

        candidates = [str(p) for p in candidate_data_dirs()]
        assert "/data" in candidates
        assert any(c.endswith("/data") and c != "/data" for c in candidates)

    def test_bundled_data_is_shipped_in_the_image(self, production_stage: str) -> None:
        """The scripts are useless without the season files."""
        assert "data ./data" in production_stage

    def test_scripts_do_not_hardcode_the_dev_mount(self) -> None:
        """Every script must resolve rather than assume."""
        offenders: list[str] = []
        for name in ("ingest.py", "backfill.py", "backtest.py", "validate.py"):
            source = (ROOT / "scripts" / name).read_text()
            if 'default=Path("/data")' in source:
                offenders.append(name)
        assert offenders == [], f"Scripts hardcoding /data: {offenders}"


class TestStackIsolation:
    """Development and production must not share state.

    Regression: both compose files used the project name ``quantsport``, so
    both resolved to the same ``quantsport_postgres-data`` volume. Tearing down
    the production stack with ``down -v`` destroyed the development database —
    hours of ingestion lost to a command that looked local to one stack.
    """

    def test_project_names_differ(self, prod_compose: dict) -> None:
        dev = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        assert prod_compose.get("name"), "production compose needs a project name"
        assert prod_compose["name"] != dev.get("name"), (
            "A shared project name means a shared volume namespace, and "
            "`down -v` on one stack destroys the other's database."
        )

    def test_production_name_is_explicit(self, prod_compose: dict) -> None:
        """Falling back to the directory name would collide again."""
        assert prod_compose["name"] == "quantsport-prod"
