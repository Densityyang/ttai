"""数据库连接管理。"""

import asyncio
import os
import re
from typing import Any

from sqlalchemy import MetaData, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.settings import get_settings
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.store.sql_utils import mask_sql_literals_and_comments

_SCHEMA_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROLE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_IDENTIFIER_PATTERN = r'(?:[A-Za-z_][A-Za-z0-9_]*|"(?:""|[^"])+")'
_SCHEMA_QUALIFIED_FROM_JOIN_PATTERN = re.compile(
    rf"\b(?:FROM|JOIN)\s+(?:ONLY\s+)?(?P<schema>{_SQL_IDENTIFIER_PATTERN})\s*\.\s*(?P<relation>{_SQL_IDENTIFIER_PATTERN})",
    re.IGNORECASE | re.DOTALL,
)


class DatabaseManager:
    """数据库访问与查询安全校验。"""

    def __init__(
        self,
        database_url: str | None = None,
        schema: str | None = None,
    ):
        settings = get_settings()
        self.database_url = database_url or settings.database_url
        self.timeout_seconds = int(os.getenv("NL2SQL_QUERY_TIMEOUT_SECONDS", "30"))
        self.max_query_results = int(os.getenv("NL2SQL_MAX_QUERY_RESULTS", "200"))

        agent_config = get_agent_config()
        default_schema = agent_config.nl2sql_db_schema
        self._schema = _normalize_schema(schema if schema is not None else default_schema)

        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._schema_info: dict[str, Any] = {}

    @property
    def schema(self) -> str | None:
        """当前 schema。"""
        return self._schema

    async def connect(self) -> None:
        """建立数据库连接并加载 schema 信息。"""
        if not self.database_url:
            raise ValueError("DATABASE_URL 未设置")
        self._engine = create_async_engine(self.database_url)
        self._session_factory = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)
        await self._load_schema_info()

    async def disconnect(self) -> None:
        """关闭数据库连接。"""
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._session_factory = None

    def session(self) -> async_sessionmaker[AsyncSession]:
        """返回可创建 session 的工厂。"""
        if self._session_factory is None:
            raise RuntimeError("DatabaseManager 未连接")
        return self._session_factory

    async def _load_schema_info(self) -> None:
        if self._engine is None:
            return

        metadata = MetaData()
        async with self._engine.begin() as conn:
            if self._schema:
                await conn.execute(text(f"SET LOCAL search_path TO {self._schema}"))
                await conn.run_sync(
                    lambda sync_conn: metadata.reflect(
                        bind=sync_conn,
                        schema=self._schema,
                        views=True,
                    )
                )
            else:
                await conn.run_sync(lambda sync_conn: metadata.reflect(bind=sync_conn, views=True))

        tables: dict[str, Any] = {}
        for table in metadata.tables.values():
            if self._schema and table.schema != self._schema:
                continue

            table_name = table.name
            if not self._schema and table.schema:
                table_name = f"{table.schema}.{table.name}"

            columns: list[dict[str, Any]] = []
            for col in table.columns:
                column_item: dict[str, Any] = {
                    "name": col.name,
                    "type": str(col.type),
                    "nullable": col.nullable,
                    "primary_key": col.primary_key,
                }
                if col.comment:
                    column_item["comment"] = col.comment
                if col.foreign_keys:
                    column_item["foreign_key"] = str(list(col.foreign_keys)[0].target_fullname)
                columns.append(column_item)

            table_info: dict[str, Any] = {
                "columns": columns,
                "schema": table.schema,
            }
            if table.comment:
                table_info["comment"] = table.comment
            tables[table_name] = table_info

        self._schema_info = {
            "schema": self._schema,
            "tables": tables,
        }

    async def _apply_search_path(self, session: AsyncSession) -> None:
        if not self._schema:
            return
        await session.execute(text(f"SET LOCAL search_path TO {self._schema}"))

    def get_table_names(self) -> list[str]:
        """获取已加载的表名。"""
        return sorted(self._schema_info.get("tables", {}).keys())

    def get_schema_description(self, table_names: list[str] | None = None) -> str:
        """获取表结构描述文本。"""
        if not self._schema_info:
            return "当前没有可用的数据库结构信息"

        selected_tables = table_names or self.get_table_names()
        schema_prefix = f"schema={self._schema} " if self._schema else ""
        lines = [f"数据库结构（{schema_prefix}共 {len(selected_tables)} 张表）："]
        for table_name in selected_tables:
            table_info = self._schema_info["tables"].get(table_name)
            if table_info is None:
                continue
            table_comment = f" ({table_info['comment']})" if table_info.get("comment") else ""
            lines.append(f"\n表：{table_name}{table_comment}")
            for col in table_info["columns"]:
                suffix = " [PK]" if col["primary_key"] else ""
                comment = f" -- {col['comment']}" if col.get("comment") else ""
                lines.append(f"  - {col['name']}: {col['type']}{suffix}{comment}")
        return "\n".join(lines)

    async def execute_query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """执行查询并返回字典结果（含并发控制）。"""
        from src.nl2sql.infra.governance.semaphore import get_concurrency_governor

        governor = get_concurrency_governor()
        async with governor.acquire("sql"):
            session_factory = self.session()
            async with session_factory() as session:
                await self._apply_search_path(session)
                result = await asyncio.wait_for(
                    session.execute(text(sql), params or {}),
                    timeout=self.timeout_seconds,
                )
                rows = result.fetchmany(self.max_query_results)
                keys = result.keys()
                return [dict(zip(keys, row)) for row in rows]

    async def validate_query(self, sql: str) -> tuple[bool, str | None]:
        """检查 SQL 是否安全且可执行（Phase 4 增强：AST 校验 + 成本估算）。"""
        from src.nl2sql.infra.governance.sql_guard import full_validate_query

        stripped_sql = sql.strip()
        if not stripped_sql:
            return False, "SQL 不能为空"

        body_sql = stripped_sql[:-1] if stripped_sql.endswith(";") else stripped_sql
        if ";" in body_sql:
            return False, "不允许执行多语句 SQL"

        normalized = stripped_sql.upper()
        if not (normalized.startswith("SELECT") or normalized.startswith("WITH")):
            return False, "仅允许执行 SELECT/CTE 查询"

        if self._schema and _has_forbidden_schema_reference(sql, allowed_schema=self._schema):
            return False, f"仅允许访问 schema `{self._schema}`"

        # Phase 4: 使用完整校验（含 AST + 笛卡尔积 + EXPLAIN 成本估算）
        session_factory = self.session()
        safe_sql, guard_err = await full_validate_query(
            stripped_sql,
            session_factory=session_factory,
            schema=self._schema,
        )
        if guard_err:
            return False, guard_err

        # 保留原有的 EXPLAIN 语法校验（成本已在 full_validate_query 中检查）
        try:
            async with session_factory() as session:
                await self._apply_search_path(session)
                await asyncio.wait_for(
                    session.execute(text(f"EXPLAIN {safe_sql}")),
                    timeout=self.timeout_seconds,
                )
            return True, None
        except TimeoutError:
            return False, f"查询校验超时（{self.timeout_seconds} 秒）"
        except Exception as exc:
            return False, str(exc)


_db_manager: DatabaseManager | None = None
_db_manager_schema: str | None = None
_db_manager_lock = asyncio.Lock()


async def get_db_manager(schema: str | None = None) -> DatabaseManager:
    """获取全局数据库管理器（单例）。"""
    global _db_manager, _db_manager_schema
    agent_config = get_agent_config()
    requested_schema = _normalize_schema(schema if schema is not None else agent_config.nl2sql_db_schema)

    if _db_manager is not None:
        if _db_manager_schema != requested_schema:
            raise ValueError(
                "全局 DatabaseManager 已按 schema="
                f"{_db_manager_schema} 初始化，当前请求 schema={requested_schema}。"
                "请重启服务后再切换。"
            )
        return _db_manager

    async with _db_manager_lock:
        if _db_manager is None:
            _db_manager = DatabaseManager(schema=requested_schema)
            await _db_manager.connect()
            _db_manager_schema = requested_schema
        elif _db_manager_schema != requested_schema:
            raise ValueError(
                "全局 DatabaseManager 已按 schema="
                f"{_db_manager_schema} 初始化，当前请求 schema={requested_schema}。"
                "请重启服务后再切换。"
            )

    return _db_manager


async def close_db_manager() -> None:
    """关闭全局数据库管理器连接。"""
    global _db_manager, _db_manager_schema
    if _db_manager is not None:
        await _db_manager.disconnect()
    _db_manager = None
    _db_manager_schema = None


async def get_nl2sql_db_manager(
    database_url: str | None = None,
    schema: str | None = None,
) -> DatabaseManager:
    """获取 NL2SQL 数据库管理器。"""
    if database_url:
        manager = DatabaseManager(database_url=database_url, schema=schema)
        await manager.connect()
        return manager
    return await get_db_manager(schema=schema)


def _normalize_schema(schema: str | None) -> str | None:
    if schema is None:
        return None

    normalized = schema.strip()
    if not normalized:
        return None

    if not _SCHEMA_IDENTIFIER_PATTERN.match(normalized):
        raise ValueError(f"非法 schema 标识符: {schema}")

    return normalized


def _has_forbidden_schema_reference(sql: str, allowed_schema: str) -> bool:
    masked_sql = mask_sql_literals_and_comments(sql)
    normalized_allowed_schema = allowed_schema.lower()

    for match in _SCHEMA_QUALIFIED_FROM_JOIN_PATTERN.finditer(masked_sql):
        schema_name = _normalize_sql_identifier(match.group("schema")).lower()
        if schema_name != normalized_allowed_schema:
            return True
    return False


def _normalize_sql_identifier(identifier: str) -> str:
    stripped = identifier.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        return stripped[1:-1].replace('""', '"')
    return stripped


def _resolve_ai_views_grantee_roles(database_url: str) -> list[str]:
    role = _extract_role_from_database_url(database_url)
    if role is None:
        return []
    return [role]


def _extract_role_from_database_url(database_url: str) -> str | None:
    try:
        url = make_url(database_url)
    except Exception:
        return None

    username = url.username
    if username is None:
        return None

    normalized = username.strip()
    if not normalized:
        return None

    if not _ROLE_IDENTIFIER_PATTERN.match(normalized):
        return None

    return normalized
