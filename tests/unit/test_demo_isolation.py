"""B3: demo synthetic authority can never be reached in product mode."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.core.auth.demo_provider import (
    DEMO_REVISION_PREFIX,
    DemoBackendAuthorizationProvider,
)
from src.core.auth.provider import resolve_authorization_context
from src.core.auth.types import AuthUser
from src.core.settings import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "auth_enabled": True,
        "tt_api_base_url": "http://auth.invalid",
    }
    base.update(overrides)
    return Settings(**base)


def test_product_mode_rejects_demo_activation_at_construction() -> None:
    """The forbidden pair must be unconstructible, not merely discouraged."""

    pg = "postgresql+asyncpg://u:p@127.0.0.1:5432/db"
    with pytest.raises(ValidationError) as excinfo:
        _settings(
            service_mode="product",
            typed_runtime_activation="demo_synthetic_authorization",
            database_url=pg,
            control_database_url=pg,
            checkpoint_database_url=pg,
        )
    assert "demo_synthetic_authorization" in str(excinfo.value)


def test_infra_dev_allows_demo_activation() -> None:
    settings = _settings(
        service_mode="infra-dev",
        typed_runtime_activation="demo_synthetic_authorization",
    )
    assert settings.typed_runtime_enabled is True
    assert settings.demo_synthetic_authorization_enabled is True


def test_default_deployment_is_unchanged() -> None:
    settings = _settings()
    assert settings.typed_runtime_activation == "disabled"
    assert settings.typed_runtime_enabled is False
    assert settings.demo_synthetic_authorization_enabled is False


async def test_known_demo_user_receives_namespaced_authority() -> None:
    provider = DemoBackendAuthorizationProvider()
    user = AuthUser(user_id="demo-analyst", telephone=None, roles=[], permissions=[])
    context = await resolve_authorization_context(provider, user)
    assert context is not None
    assert context.authorization_revision.startswith(DEMO_REVISION_PREFIX)
    assert context.allowed_scope_ids == ("demo-team-1",)


async def test_unknown_real_user_never_receives_demo_authority() -> None:
    provider = DemoBackendAuthorizationProvider()
    for user_id in ("real-alice", "admin", "*", "", "demo-analyst "):
        user = AuthUser(user_id=user_id, telephone=None, roles=["admin"], permissions=["*"])
        assert await resolve_authorization_context(provider, user) is None


async def test_role_and_org_never_influence_demo_authority() -> None:
    from src.core.auth.types import OrganizationIdentity

    provider = DemoBackendAuthorizationProvider()
    # an unknown user with every privilege signal still gets nothing
    user = AuthUser(
        user_id="outsider",
        telephone=None,
        roles=["business_admin", "superuser"],
        permissions=["*", "*.*.*"],
        organization=OrganizationIdentity(company_id="1", team_id="1"),
    )
    assert await resolve_authorization_context(provider, user) is None


def test_demo_provider_is_never_a_container_default() -> None:
    """No module-level or class-level demo instance may exist."""

    import src.nl2sql.container as container_module

    assert not hasattr(container_module, "_demo_provider")
    assert not hasattr(container_module, "DEMO_PROVIDER")


def test_block_manifest_covers_the_mode_suggestion_block() -> None:
    from src.nl2sql.v2 import BLOCK_MANIFEST, UNSUPPORTED_BLOCK_FALLBACK

    for block_type in (
        "text",
        "metric_card",
        "table",
        "clarification",
        "plan_card",
        "mode_suggestion",
        "conflict_comparison",
        "provenance",
        "definition",
    ):
        assert block_type in BLOCK_MANIFEST
    assert UNSUPPORTED_BLOCK_FALLBACK["kind"] == "unsupported_block"
