from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from src.nl2sql.container import AppContainer


class _Engine:
    def __init__(self) -> None:
        self.dispose_count = 0

    async def dispose(self) -> None:
        self.dispose_count += 1


class _Closeable:
    def __init__(self) -> None:
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


@pytest.mark.asyncio
async def test_local_real_probe_cancellation_disposes_both_engines_and_resets_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy.ext.asyncio

    import src.core.database as core_database
    import src.core.settings as core_settings
    import src.nl2sql.config.settings as agent_settings
    import src.nl2sql.infra.governance.query_gateway as query_gateway
    import src.nl2sql.infra.store.ai_views as ai_views
    import src.nl2sql.local_real.live_probe as live_probe

    engines: list[_Engine] = []
    settings = SimpleNamespace(database_url="postgresql+asyncpg://reader@localhost/db")
    config = SimpleNamespace(
        ai_views_config_path="views.yaml",
        nl2sql_db_schema="ai_views",
    )

    def create_engine(*_: Any, **__: Any) -> _Engine:
        engine = _Engine()
        engines.append(engine)
        return engine

    async def cancel_probe(*_: Any, **__: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(core_settings, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: config)
    monkeypatch.setattr(core_database, "create_runtime_async_engine", create_engine)
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "async_sessionmaker", lambda *_, **__: object())
    monkeypatch.setattr(query_gateway, "QueryGateway", lambda *_, **__: object())
    monkeypatch.setattr(ai_views, "load_ai_views_config", lambda *_: SimpleNamespace(target_schema="ai_views"))
    monkeypatch.setattr(live_probe, "probe_relation", cancel_probe)

    container = AppContainer()
    with pytest.raises(asyncio.CancelledError):
        await container._local_real_deployment_inputs()

    assert len(engines) == 2
    assert [engine.dispose_count for engine in engines] == [1, 1]
    assert container._local_real_probe_attempted is False
    assert container._local_real_deployment is None
    assert container._local_real_resources is None


@pytest.mark.asyncio
async def test_typed_runtime_cancellation_closes_partial_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.database as core_database
    import src.core.settings as core_settings
    import src.nl2sql.config.settings as agent_settings
    import src.nl2sql.infra.store.ai_views as ai_views
    import src.nl2sql.semantic.registry as semantic_registry
    import src.nl2sql.semantic.schema_snapshot as schema_snapshot

    business_engine = _Engine()
    publisher = _Closeable()
    snapshots = _Closeable()
    settings = SimpleNamespace(
        control_database_url="postgresql://control/db",
        database_url="postgresql+asyncpg://reader@localhost/db",
    )
    config = SimpleNamespace(
        ai_views_config_path="views.yaml",
        nl2sql_db_schema="ai_views",
    )
    engine_calls = 0

    def create_engine(*_: Any, **__: Any) -> _Engine:
        nonlocal engine_calls
        engine_calls += 1
        if engine_calls == 1:
            return business_engine
        raise asyncio.CancelledError

    monkeypatch.setattr(core_settings, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: config)
    monkeypatch.setattr(core_database, "create_runtime_async_engine", create_engine)
    monkeypatch.setattr(ai_views, "load_ai_views_config", lambda *_: object())
    monkeypatch.setattr(semantic_registry, "ControlSemanticReleasePublisher", lambda *_: publisher)
    monkeypatch.setattr(schema_snapshot, "ControlSchemaSnapshotStore", lambda *_: snapshots)

    container = AppContainer()
    with pytest.raises(asyncio.CancelledError):
        await container._typed_deployment_inputs()

    assert business_engine.dispose_count == 1
    assert publisher.close_count == 1
    assert snapshots.close_count == 1
    assert container._typed_deployment is None
    assert container._typed_resources is None


@pytest.mark.asyncio
async def test_database_manager_disposes_partial_engines_after_cancelled_schema_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.settings as core_settings
    import src.nl2sql.config.settings as agent_settings
    import src.nl2sql.infra.store.database as database

    engines: list[_Engine] = []
    settings = SimpleNamespace(
        database_url="postgresql+asyncpg://reader@localhost/db",
        database_lock_timeout_ms=1000,
        database_idle_transaction_timeout_ms=1000,
    )
    config = SimpleNamespace(nl2sql_db_schema="ai_views")

    def create_engine(*_: Any, **__: Any) -> _Engine:
        engine = _Engine()
        engines.append(engine)
        return engine

    async def cancel_schema_load() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(core_settings, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: config)
    monkeypatch.setattr(database, "create_runtime_async_engine", create_engine)
    monkeypatch.setattr(database, "async_sessionmaker", lambda *_, **__: object())
    monkeypatch.setattr(database, "QueryGateway", lambda *_, **__: object())

    manager = database.DatabaseManager(schema="ai_views")
    monkeypatch.setattr(manager, "_load_schema_info", cancel_schema_load)
    with pytest.raises(asyncio.CancelledError):
        await manager.connect()

    assert len(engines) == 2
    assert [engine.dispose_count for engine in engines] == [1, 1]
    assert manager._engine is None
    assert manager._cancellation_engine is None
    assert manager._session_factory is None
    assert manager._query_gateway is None


@pytest.mark.asyncio
async def test_database_manager_disposes_first_engine_if_second_engine_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.settings as core_settings
    import src.nl2sql.config.settings as agent_settings
    import src.nl2sql.infra.store.database as database

    business_engine = _Engine()
    settings = SimpleNamespace(
        database_url="postgresql+asyncpg://reader@localhost/db",
        database_lock_timeout_ms=1000,
        database_idle_transaction_timeout_ms=1000,
    )
    config = SimpleNamespace(nl2sql_db_schema="ai_views")
    create_count = 0

    def create_engine(*_: Any, **__: Any) -> _Engine:
        nonlocal create_count
        create_count += 1
        if create_count == 1:
            return business_engine
        raise RuntimeError("cancel engine initialization failed")

    monkeypatch.setattr(core_settings, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: config)
    monkeypatch.setattr(database, "create_runtime_async_engine", create_engine)

    manager = database.DatabaseManager(schema="ai_views")
    with pytest.raises(RuntimeError, match="cancel engine initialization failed"):
        await manager.connect()

    assert business_engine.dispose_count == 1
    assert manager._engine is None
    assert manager._cancellation_engine is None
    assert manager._session_factory is None
    assert manager._query_gateway is None


@pytest.mark.asyncio
async def test_failed_db_manager_connect_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.nl2sql.config.settings as agent_settings
    import src.nl2sql.infra.store.database as database

    attempts = 0

    class _Manager:
        def __init__(self, *, schema: str | None = None) -> None:
            self.schema = schema

        async def connect(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("initialization failed")

    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: SimpleNamespace(nl2sql_db_schema="ai_views"))
    monkeypatch.setattr(database, "DatabaseManager", _Manager)
    monkeypatch.setattr(database, "_db_manager", None)
    monkeypatch.setattr(database, "_db_manager_schema", None)

    with pytest.raises(RuntimeError, match="initialization failed"):
        await database.get_db_manager()
    assert database._db_manager is None
    assert database._db_manager_schema is None

    manager = await database.get_db_manager()
    assert isinstance(manager, _Manager)
    assert database._db_manager is manager
    assert database._db_manager_schema == "ai_views"
