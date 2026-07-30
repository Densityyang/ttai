from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from main import create_app
from src.core.settings import get_settings
from src.nl2sql.container import AppContainer


def test_infra_dev_is_ready_without_a_model(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_MODE", "infra-dev")
    monkeypatch.setenv("MODEL_REQUIRED", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["components"]["model"] == {
            "status": "unavailable",
            "required": False,
        }
        assert response.json()["components"]["checkpoint"]["status"] == "ready"
    finally:
        get_settings.cache_clear()


def test_product_requires_a_model_for_readiness(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_MODE", "product")
    monkeypatch.setenv("MODEL_REQUIRED", "true")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://business_reader:test@business.invalid/business",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
    finally:
        get_settings.cache_clear()


def test_product_rejects_process_local_checkpoint_backend(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_MODE", "product")
    monkeypatch.setenv("MODEL_REQUIRED", "true")
    monkeypatch.setenv("MEMORY_BACKEND", "memory")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://business_reader:test@business.invalid/business",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-for-capability-test")
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    monkeypatch.delenv("CONTROL_DATABASE_URL_FILE", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            response = client.get("/readyz")
        assert response.status_code == 503
        payload = response.json()
        assert payload["components"]["model"]["status"] == "ready"
        assert payload["components"]["checkpoint"]["status"] == "unavailable"
        assert "product_checkpoint_backend_not_durable" in payload["degradation_reasons"]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_container_never_runs_checkpoint_migrations_at_startup(monkeypatch) -> None:
    import src.nl2sql.container as container_module

    class FakeCheckpointerManager:
        def __init__(self) -> None:
            self.setup: bool | None = None

        async def init(self, *, setup: bool = True) -> None:
            self.setup = setup

        async def close(self) -> None:
            pass

    fake_manager = FakeCheckpointerManager()
    monkeypatch.setattr(container_module, "CheckpointerManager", lambda: fake_manager)
    container = AppContainer()
    await container.start()
    assert fake_manager.setup is False


def test_api_lifespan_never_runs_migrations_or_index_builds() -> None:
    from src.nl2sql import api

    source = inspect.getsource(api.lifespan)
    for forbidden_call in (
        "sync_qa_index(",
        "sync_semantic_index(",
        "run_indexer(",
        "warmup_runtime(",
        "create_all(",
    ):
        assert forbidden_call not in source


def test_dependency_failure_keeps_liveness_but_fails_readiness(monkeypatch) -> None:
    import src.nl2sql.container as container_module

    class FailingCheckpointerManager:
        async def init(self, *, setup: bool = True) -> None:
            del setup
            raise ConnectionError("sensitive endpoint must not be returned")

        async def close(self) -> None:
            pass

    monkeypatch.setenv("SERVICE_MODE", "infra-dev")
    monkeypatch.setenv("MODEL_REQUIRED", "false")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(container_module, "CheckpointerManager", FailingCheckpointerManager)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/healthz").status_code == 200
            response = client.get("/readyz")
        assert response.status_code == 503
        payload = response.json()
        assert payload["components"]["checkpoint"]["status"] == "unavailable"
        assert "sensitive endpoint" not in str(payload)
    finally:
        get_settings.cache_clear()


def test_cors_uses_explicit_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_MODE", "infra-dev")
    monkeypatch.setenv("MODEL_REQUIRED", "false")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://allowed.example")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            allowed = client.options(
                "/healthz",
                headers={
                    "Origin": "https://allowed.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            denied = client.options(
                "/healthz",
                headers={
                    "Origin": "https://denied.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://allowed.example"
        assert "access-control-allow-origin" not in denied.headers
    finally:
        get_settings.cache_clear()
