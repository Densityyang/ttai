"""Asynchronous database tools exposed to SQL-capable agents."""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from src.nl2sql.infra.store.database import DatabaseManager

logger = logging.getLogger(__name__)


def create_async_sql_tools(db_manager: DatabaseManager) -> list[BaseTool]:
    """Create the table, schema and governed query tools for an agent."""

    @tool("sql_db_list_tables")
    async def list_tables() -> str:
        """列出当前允许访问的数据视图。"""

        try:
            table_names = db_manager.get_table_names()
            if not table_names:
                return "数据库中没有可用数据视图"
            schema_note = f" (schema={db_manager.schema})" if db_manager.schema else ""
            return f"{', '.join(table_names)}{schema_note}"
        except Exception as exc:
            _log_tool_failure("list_tables", exc)
            return "获取数据视图列表失败: internal database metadata error"

    @tool("sql_db_schema")
    async def get_schema(table_names: str) -> str:
        """获取逗号分隔的数据视图字段、类型和主键信息。"""

        try:
            tables = [name.strip() for name in table_names.split(",") if name.strip()]
            if not tables:
                return "错误: 必须提供至少一个数据视图名称"
            return db_manager.get_schema_description(tables)
        except Exception as exc:
            _log_tool_failure("get_schema", exc)
            return "获取数据视图结构失败: internal database metadata error"

    @tool("sql_db_query")
    async def query(query: str) -> str:
        """通过 QueryGateway 执行一个只读 SELECT，并返回有界、脱敏的结果。"""

        try:
            receipt = await db_manager.query(query)
        except Exception as exc:
            _log_tool_failure("query", exc)
            return "查询执行失败 [code=database_error retryable=false]: internal query gateway error"

        if not receipt.accepted:
            assert receipt.error is not None
            retryable = str(receipt.error.retryable).lower()
            return (
                "查询验证失败 "
                f"[code={receipt.error.code} retryable={retryable}]: "
                f"{receipt.error.message}"
            )
        if not receipt.rows:
            return "查询成功,但没有返回结果"
        return str(receipt.rows[0] if len(receipt.rows) == 1 else receipt.rows)

    return [list_tables, get_schema, query]


def _log_tool_failure(stage: str, exc: Exception) -> None:
    # Do not emit the exception message: driver errors can carry SQL literals or
    # connection details.  The gateway already records a redacted fingerprint.
    logger.warning("database tool failure stage=%s error_type=%s", stage, type(exc).__name__)
