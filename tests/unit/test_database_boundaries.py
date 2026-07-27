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


def test_checkpointer_uses_dedicated_database_url() -> None:
    source = (ROOT / "src/nl2sql/infra/memory/checkpointer.py").read_text(encoding="utf-8")
    assert "checkpoint_database_url" in source
    assert "memory_backend_url" not in source


def test_development_databases_use_independent_volumes() -> None:
    compose = yaml.safe_load((ROOT / "docker/compose.dev.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["control-postgres"]["volumes"] == ["control_pgdata:/var/lib/postgresql/data"]
    assert services["checkpoint-postgres"]["volumes"] == [
        "checkpoint_pgdata:/var/lib/postgresql/data"
    ]
    assert services["business-postgres"]["volumes"] == ["business_pgdata:/var/lib/postgresql/data"]
    assert {"migrate", "backup", "restore-test"}.issubset(services)
    assert services["migrate"]["profiles"] == ["ops"]


def test_product_profile_has_no_local_business_database() -> None:
    compose = yaml.safe_load((ROOT / "docker/compose.prod.yml").read_text(encoding="utf-8"))
    assert "business-postgres" not in compose["services"]
    assert compose["networks"]["business_external_net"]["external"] is True
