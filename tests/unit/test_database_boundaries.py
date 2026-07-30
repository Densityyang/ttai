from __future__ import annotations

from pathlib import Path

import yaml

from src.core.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_api_settings_exclude_admin_database_url() -> None:
    assert "database_url_admin" not in Settings.model_fields
    assert {"database_url", "control_database_url", "checkpoint_database_url"}.issubset(
        Settings.model_fields
    )


def test_database_manager_does_not_run_schema_mutations() -> None:
    source = (ROOT / "src/nl2sql/infra/store/database.py").read_text(encoding="utf-8")
    assert "database_url_admin" not in source
    assert "sync_ai_views_from_yaml" not in source
    assert "DatabasePurpose.BUSINESS_READ_ONLY" in source
    assert "create_runtime_async_engine" in source


def test_checkpointer_uses_dedicated_database_url() -> None:
    source = (ROOT / "src/nl2sql/infra/memory/checkpointer.py").read_text(encoding="utf-8")
    assert "checkpoint_database_url" in source
    assert "memory_backend_url" not in source
    assert "setup: bool = False" in source


def test_direct_control_audit_pool_validates_application_role() -> None:
    source = (ROOT / "src/nl2sql/observability/control_audit.py").read_text(encoding="utf-8")
    assert "validate_application_database_url" in source
    assert "DatabasePurpose.CONTROL_APP" in source


def test_development_databases_use_independent_volumes() -> None:
    compose = yaml.safe_load((ROOT / "docker/compose.dev.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "control_pgdata:/var/lib/postgresql/data" in services["control-postgres"]["volumes"]
    assert "checkpoint_pgdata:/var/lib/postgresql/data" in services["checkpoint-postgres"][
        "volumes"
    ]
    assert "business_pgdata:/var/lib/postgresql/data" in services["business-postgres"][
        "volumes"
    ]
    for service_name in ("control-postgres", "checkpoint-postgres", "business-postgres"):
        assert "./initdb/roles.sh:/docker-entrypoint-initdb.d/010-roles.sh:ro" in services[
            service_name
        ]["volumes"]
    assert {"migrate", "backup", "restore-test"}.issubset(services)
    assert services["migrate"]["profiles"] == ["ops"]


def test_api_services_never_receive_privileged_database_secrets() -> None:
    for compose_file in ("docker/compose.prod.yml", "docker/compose.release.yml"):
        compose = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
        for service_name in ("api-a", "api-b"):
            mounted_secrets = set(compose["services"][service_name]["secrets"])
            assert not any(
                token in secret_name
                for secret_name in mounted_secrets
                for token in ("owner", "migrator", "backup", "restore")
            )


def test_product_profile_has_no_local_business_database() -> None:
    compose = yaml.safe_load((ROOT / "docker/compose.prod.yml").read_text(encoding="utf-8"))
    assert "business-postgres" not in compose["services"]
    assert compose["networks"]["business_external_net"]["external"] is True
