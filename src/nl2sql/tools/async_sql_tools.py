"""异步 SQL 工具集"""

from langchain_core.tools import BaseTool, tool

from src.nl2sql.infra.store.database import DatabaseManager


def create_async_sql_tools(db_manager: DatabaseManager) -> list[BaseTool]:
    """创建异步 SQL 工具集

    参数:
        db_manager: 数据库管理器实例

    返回:
        异步工具列表,工具名称与 SQLDatabaseToolkit 保持一致
    """

    @tool("sql_db_list_tables")
    async def list_tables() -> str:
        """列出数据库中的所有表名"""
        try:
            table_names = db_manager.get_table_names()
            if not table_names:
                return "数据库中没有表"
            schema_note = f" (schema={db_manager.schema})" if db_manager.schema else ""
            return f"{', '.join(table_names)}{schema_note}"
        except Exception as e:
            return f"获取表列表失败: {e}"

    @tool("sql_db_schema")
    async def get_schema(table_names: str) -> str:
        """获取指定表的结构信息,包括列名、类型、主键等。

        Args:
            table_names: 逗号分隔的表名列表
        """
        try:
            tables = [t.strip() for t in table_names.split(",") if t.strip()]
            if not tables:
                return "错误: 必须提供至少一个表名"

            schema = db_manager.get_schema_description(tables)
            return schema
        except Exception as e:
            return f"获取表结构失败: {e}"

    @tool("sql_db_query")
    async def query(query: str) -> str:
        """执行 SELECT 查询并返回结果。仅支持 SELECT 语句,不允许 UPDATE/DELETE/DROP 等操作。

        Args:
            query: SQL 查询语句 (仅支持 SELECT)
        """
        try:
            # 验证查询安全性
            is_valid, error_msg = await db_manager.validate_query(query)
            if not is_valid:
                return f"查询验证失败: {error_msg}"

            # 执行查询
            results = await db_manager.execute_query(query)

            if not results:
                return "查询成功,但没有返回结果"

            # 格式化结果
            if len(results) == 1:
                return str(results[0])

            return str(results)
        except Exception as e:
            return f"查询执行失败: {e}"

    return [list_tables, get_schema, query]
