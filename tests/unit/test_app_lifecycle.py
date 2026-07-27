from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import create_app
from src.core.settings import get_settings
from src.nl2sql.container import AppContainer


def test_infra_dev_is_ready_without_a_model(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_MODE", "infra-dev")
    monkeypatch.setenv("MODEL_REQUIRED", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["components"]["model"] == "unavailable"
    finally:
        get_settings.cache_clear()


def test_product_requires_a_model_for_readiness(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_MODE", "product")
    monkeypatch.setenv("MODEL_REQUIRED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
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
