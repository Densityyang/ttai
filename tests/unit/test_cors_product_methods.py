"""CORS must permit the product PATCH preflight a real browser sends."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

ORIGIN = "http://localhost:5173"


@pytest.fixture()
def app_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ORIGIN)
    from src.core.settings import get_settings

    get_settings.cache_clear()
    from main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_definition_patch_preflight_advertises_patch(app_client: TestClient) -> None:
    """Without PATCH the real browser product mutation would fail CORS."""

    response = app_client.options(
        "/api/v2/nl2sql/definitions/def_" + "a" * 32 + "/draft",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200, response.text
    allowed = response.headers["access-control-allow-methods"]
    assert "PATCH" in allowed, allowed
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_allowed_methods_stay_explicit(app_client: TestClient) -> None:
    """The product CORS surface must never degrade into a wildcard."""

    response = app_client.options(
        "/api/v2/nl2sql/library/install",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    allowed = response.headers["access-control-allow-methods"]
    assert "*" not in allowed
    for method in ("GET", "POST", "PATCH", "OPTIONS"):
        assert method in allowed, (method, allowed)
    # DELETE is deliberately NOT part of the product surface.
    assert "DELETE" not in allowed


def test_unlisted_origin_is_not_reflected(app_client: TestClient) -> None:
    response = app_client.options(
        "/api/v2/nl2sql/definitions/def_" + "a" * 32 + "/draft",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "PATCH",
        },
    )
    assert response.headers.get("access-control-allow-origin") != "http://evil.example"