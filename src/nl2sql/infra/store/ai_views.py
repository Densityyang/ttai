"""ai 视图 YAML 配置同步。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.core.settings import ROOT_DIR
from src.nl2sql.infra.store.sql_utils import find_sql_comment_marker

logger = logging.getLogger(__name__)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")

type ScalarValue = str | int | float | bool | None
type RelationRef = tuple[str | None, str]


def _validate_identifier(value: str, *, field_name: str) -> str:
    """验证并规范化 SQL 标识符。

    Args:
        value: 待验证的标识符字符串。
        field_name: 字段名称，用于错误消息。

    Returns:
        规范化后的标识符（去除首尾空白）。

    Raises:
        ValueError: 标识符为空或格式非法。
    """
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    if not _IDENTIFIER_PATTERN.match(normalized):
        raise ValueError(f"{field_name} 非法: {value}")
    return normalized


def _validate_sql_fragment(value: str, *, field_name: str) -> str:
    """验证并规范化 SQL 片段。

    检查 SQL 片段是否包含非法字符（分号、注释标记等），
    以防止 SQL 注入。

    Args:
        value: 待验证的 SQL 片段。
        field_name: 字段名称，用于错误消息。

    Returns:
        规范化后的 SQL 片段（去除首尾空白）。

    Raises:
        ValueError: SQL 片段为空或包含非法字符。
    """
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    if ";" in normalized:
        raise ValueError(f"{field_name} 含非法片段: ;")

    forbidden_comment = find_sql_comment_marker(normalized)
    if forbidden_comment is not None:
        raise ValueError(f"{field_name} 含非法片段: {forbidden_comment}")

    return normalized

class FilterCondition(BaseModel):
    """行过滤条件。"""

    column: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "like", "ilike", "is_null", "is_not_null"]
    value: ScalarValue | list[ScalarValue] | None = None

    @field_validator("column")
    @classmethod
    def validate_column(cls, value: str) -> str:
        normalized = value.strip()
        if not _QUALIFIED_IDENTIFIER_PATTERN.match(normalized):
            raise ValueError(f"filters.column 非法: {value}")
        return normalized

    @model_validator(mode="after")
    def validate_value(self) -> "FilterCondition":
        if self.operator in {"is_null", "is_not_null"}:
            return self

        if self.value is None:
            raise ValueError(f"filters.operator={self.operator} 必须提供 value")

        if self.operator in {"in", "not_in"}:
            if not isinstance(self.value, list):
                raise ValueError(f"filters.operator={self.operator} 的 value 必须是数组")
            if not self.value:
                raise ValueError(f"filters.operator={self.operator} 的 value 不能为空数组")
        elif isinstance(self.value, list):
            raise ValueError(f"filters.operator={self.operator} 的 value 不能是数组")

        return self


class ViewDefinition(BaseModel):
    """单个视图定义。"""

    name: str
    description: str | None = None
    source_table: str
    source_schema: str | None = None
    source_alias: str | None = None
    columns: list[str] = Field(default_factory=list)
    column_comments: dict[str, str] = Field(default_factory=dict)
    filters: list[FilterCondition] = Field(default_factory=list)
    joins: list["JoinDefinition"] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_identifier(value, field_name="views.name")

    @field_validator("source_table")
    @classmethod
    def validate_source_table(cls, value: str) -> str:
        return _validate_identifier(value, field_name="views.source_table")

    @field_validator("source_schema")
    @classmethod
    def validate_source_schema(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name="views.source_schema")

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("views.columns 不能为空")
        return [_validate_sql_fragment(column, field_name="views.columns") for column in value]

    @field_validator("source_alias")
    @classmethod
    def validate_source_alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name="views.source_alias")


class JoinDefinition(BaseModel):
    """视图 JOIN 定义。"""

    model_config = ConfigDict(populate_by_name=True)

    table: str
    join_schema: str | None = Field(default=None, alias="schema")
    alias: str | None = None
    join_condition: str = Field(alias="on")
    type: Literal["inner", "left", "right", "full"] = "left"

    @field_validator("table")
    @classmethod
    def validate_table(cls, value: str) -> str:
        return _validate_identifier(value, field_name="views.joins.table")

    @field_validator("join_schema")
    @classmethod
    def validate_schema(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name="views.joins.schema")

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name="views.joins.alias")

    @field_validator("join_condition")
    @classmethod
    def validate_on(cls, value: str) -> str:
        return _validate_sql_fragment(value, field_name="views.joins.on")


class AIViewsConfig(BaseModel):
    """ai 视图配置。"""

    model_config = ConfigDict(populate_by_name=True)

    target_schema: str = Field(default="ai_views", alias="schema")
    views: list[ViewDefinition] = Field(default_factory=list)

    @field_validator("target_schema")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        return _validate_identifier(value, field_name="schema")

    @field_validator("views")
    @classmethod
    def validate_views(cls, value: list[ViewDefinition]) -> list[ViewDefinition]:
        if not value:
            raise ValueError("views 不能为空")
        return value


def _resolve_config_path(config_path: str) -> Path:
    """解析配置文件路径。

    将相对路径转换为基于项目根目录的绝对路径，
    绝对路径则直接返回。

    Args:
        config_path: 配置文件路径（相对或绝对）。

    Returns:
        解析后的绝对路径。
    """
    path = Path(config_path)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def _render_literal(value: ScalarValue) -> str:
    """将标量值渲染为 SQL 字面量字符串。

    Args:
        value: 标量值（字符串、整数、浮点数、布尔值或 None）。

    Returns:
        SQL 字面量字符串，如 'string'、123、TRUE、NULL 等。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _build_where_clause(filters: list[FilterCondition]) -> str:
    """根据过滤条件构建 SQL WHERE 子句。

    Args:
        filters: 过滤条件列表。

    Returns:
        WHERE 子句字符串，若无过滤条件则返回空字符串。
    """
    if not filters:
        return ""

    fragments: list[str] = []
    op_mapping = {
        "eq": "=",
        "ne": "<>",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "like": "LIKE",
        "ilike": "ILIKE",
    }

    for item in filters:
        column = item.column
        operator = item.operator
        if operator == "is_null":
            fragments.append(f"{column} IS NULL")
            continue
        if operator == "is_not_null":
            fragments.append(f"{column} IS NOT NULL")
            continue
        if operator in {"in", "not_in"}:
            values = item.value
            if not isinstance(values, list):
                raise ValueError(f"filters.{column} 配置错误: in/not_in 需要数组")
            rendered_values = ", ".join(_render_literal(v) for v in values)
            sql_operator = "IN" if operator == "in" else "NOT IN"
            fragments.append(f"{column} {sql_operator} ({rendered_values})")
            continue

        if isinstance(item.value, list):
            raise ValueError(f"filters.{column} 配置错误: {operator} 不支持数组值")
        sql_operator = op_mapping[operator]
        fragments.append(f"{column} {sql_operator} {_render_literal(item.value)}")

    return " WHERE " + " AND ".join(fragments)


def _qualified_table_name(schema: str | None, table: str) -> str:
    """构建带 schema 前缀的完整表名。

    Args:
        schema: schema 名称，可为 None。
        table: 表名。

    Returns:
        完整表名，如 "schema.table" 或 "table"。
    """
    if schema:
        return f"{schema}.{table}"
    return table


def _extract_output_column_name(column_expr: str) -> str:
    """从列表达式中提取输出列名。

    例: "sfo.id" -> "id", "area.name AS area_name" -> "area_name"
    """
    upper = column_expr.upper()
    as_pos = upper.rfind(" AS ")
    if as_pos != -1:
        return column_expr[as_pos + 4 :].strip()
    dot_pos = column_expr.rfind(".")
    if dot_pos != -1:
        return column_expr[dot_pos + 1 :].strip()
    return column_expr.strip()


def _extract_source_column_ref(column_expr: str) -> tuple[str | None, str] | None:
    """提取列表达式中的来源字段引用。

    返回:
        (relation_alias_or_table, column_name)。
        - relation_alias_or_table 为 None 时表示默认来源表字段。
        - 无法安全解析时返回 None。
    """

    upper = column_expr.upper()
    as_pos = upper.rfind(" AS ")
    source_expr = column_expr[:as_pos].strip() if as_pos != -1 else column_expr.strip()
    if not source_expr:
        return None

    dotted_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", source_expr)
    if dotted_match:
        return dotted_match.group(1), dotted_match.group(2)

    if _IDENTIFIER_PATTERN.fullmatch(source_expr):
        return None, source_expr

    return None


def _build_relation_lookup(definition: ViewDefinition) -> tuple[RelationRef, dict[str, RelationRef]]:
    """构建视图中可用于字段解析的 relation 映射。"""

    source_relation: RelationRef = (definition.source_schema, definition.source_table)
    relation_lookup: dict[str, RelationRef] = {
        definition.source_table: source_relation,
    }
    if definition.source_alias:
        relation_lookup[definition.source_alias] = source_relation

    for join in definition.joins:
        relation: RelationRef = (join.join_schema, join.table)
        relation_lookup[join.table] = relation
        if join.alias:
            relation_lookup[join.alias] = relation

    return source_relation, relation_lookup


async def _load_relation_comments(
    conn: AsyncConnection,
    relation_lookup: dict[str, RelationRef],
) -> dict[RelationRef, dict[str, str]]:
    """批量读取 relation 字段注释（一次查询所有表）。"""

    # 去重获取所有需要查询的 relation
    unique_relations = list(set(relation_lookup.values()))
    if not unique_relations:
        return {}

    # 构建 qualified name 列表
    relation_names = [f"{schema}.{table}" if schema else table for schema, table in unique_relations]

    # 一次性查询所有表的字段注释
    result = await conn.execute(
        text(
            """
            SELECT
                c.relnamespace::regnamespace::text AS schema_name,
                c.relname AS table_name,
                a.attname AS column_name,
                d.description AS column_comment
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_attribute AS a
                ON a.attrelid = c.oid
            LEFT JOIN pg_catalog.pg_description AS d
                ON d.objoid = a.attrelid
                AND d.objsubid = a.attnum
            WHERE c.oid = ANY(
                SELECT pg_catalog.to_regclass(unnest(CAST(:relation_names AS text[])))
            )
                AND a.attnum > 0
                AND NOT a.attisdropped
            """
        ),
        {"relation_names": relation_names},
    )

    # 按 relation 分组整理结果
    relation_comments: dict[RelationRef, dict[str, str]] = {relation: {} for relation in unique_relations}

    for row in result.mappings():
        schema = row["schema_name"]
        table = row["table_name"]
        column_name = row["column_name"]
        column_comment = row["column_comment"]

        if isinstance(column_name, str) and isinstance(column_comment, str) and column_comment:
            relation = (schema, table)
            if relation in relation_comments:
                relation_comments[relation][column_name] = column_comment

    return relation_comments


def _infer_column_comment_from_relations(
    *,
    column_expr: str,
    source_relation: RelationRef,
    relation_lookup: dict[str, RelationRef],
    relation_comments: dict[RelationRef, dict[str, str]],
) -> str | None:
    """根据列表达式推断字段注释。"""

    source_ref = _extract_source_column_ref(column_expr)
    if source_ref is None:
        return None

    relation_hint, column_name = source_ref
    if relation_hint is None:
        return relation_comments.get(source_relation, {}).get(column_name)

    relation = relation_lookup.get(relation_hint)
    if relation is None:
        return None
    return relation_comments.get(relation, {}).get(column_name)


def _build_view_sql(*, target_schema: str, definition: ViewDefinition) -> str:
    """根据视图定义构建 CREATE VIEW SQL 语句。

    Args:
        target_schema: 目标 schema 名称。
        definition: 视图定义对象。

    Returns:
        完整的 CREATE OR REPLACE VIEW SQL 语句。
    """
    source = _qualified_table_name(definition.source_schema, definition.source_table)
    source_with_alias = f"{source} {definition.source_alias}" if definition.source_alias else source

    join_mapping = {
        "inner": "INNER JOIN",
        "left": "LEFT JOIN",
        "right": "RIGHT JOIN",
        "full": "FULL JOIN",
    }
    join_clauses: list[str] = []
    for join in definition.joins:
        join_source = _qualified_table_name(join.join_schema, join.table)
        join_source_with_alias = f"{join_source} {join.alias}" if join.alias else join_source
        join_keyword = join_mapping[join.type]
        join_clauses.append(f"{join_keyword} {join_source_with_alias} ON {join.join_condition}")

    columns = ", ".join(definition.columns)
    joins_sql = f" {' '.join(join_clauses)}" if join_clauses else ""
    where_clause = _build_where_clause(definition.filters)
    return (
        f"CREATE OR REPLACE VIEW {target_schema}.{definition.name} AS "
        f"SELECT {columns} FROM {source_with_alias}{joins_sql}{where_clause}"
    )


def _load_config(config_path: str) -> AIViewsConfig:
    """从 YAML 文件加载 AI 视图配置。

    Args:
        config_path: 配置文件路径（相对或绝对）。

    Returns:
        解析后的 AIViewsConfig 对象。

    Raises:
        FileNotFoundError: 配置文件不存在。
        ValueError: 配置文件格式错误。
    """
    resolved_path = _resolve_config_path(config_path)
    if not resolved_path.exists():
        raise FileNotFoundError(f"ai 视图配置文件不存在: {resolved_path}")

    with resolved_path.open("r", encoding="utf-8") as file:
        raw_data = yaml.safe_load(file)

    if not isinstance(raw_data, dict):
        raise ValueError(f"ai 视图配置格式错误: {resolved_path}")

    return AIViewsConfig.model_validate(raw_data)


def _normalize_grantee_roles(roles: list[str] | None) -> list[str]:
    """规范化并去重授权角色列表。

    Args:
        roles: 角色名称列表，可为 None 或空。

    Returns:
        去重后的规范化角色名称列表。
    """
    if not roles:
        return []

    deduplicated: list[str] = []
    seen: set[str] = set()
    for role in roles:
        normalized = _validate_identifier(role, field_name="grantee_roles")
        if normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(normalized)
    return deduplicated


def _build_grant_sqls(*, target_schema: str, owner_role: str, grantee_roles: list[str]) -> list[str]:
    """构建授权 SQL 语句列表。

    为每个被授权角色生成：
    - schema 的 USAGE 权限
    - schema 下所有表的 SELECT 权限
    - 默认权限设置（自动授权新创建的表）

    Args:
        target_schema: 目标 schema 名称。
        owner_role: schema 所有者角色。
        grantee_roles: 被授权角色列表。

    Returns:
        GRANT SQL 语句列表。
    """
    schema_name = _validate_identifier(target_schema, field_name="schema")
    owner = _validate_identifier(owner_role, field_name="owner_role")

    sqls: list[str] = []
    for grantee in _normalize_grantee_roles(grantee_roles):
        sqls.extend(
            [
                f"GRANT USAGE ON SCHEMA {schema_name} TO {grantee}",
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema_name} TO {grantee}",
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema_name} "
                    f"GRANT SELECT ON TABLES TO {grantee}"
                ),
            ]
        )
    return sqls


async def sync_ai_views_from_yaml(
    *,
    engine: AsyncEngine,
    config_path: str,
    expected_schema: str | None,
    grantee_roles: list[str] | None = None,
) -> None:
    """根据 YAML 配置自动创建/更新 ai 视图。"""

    config = _load_config(config_path)

    if expected_schema and config.target_schema != expected_schema:
        raise ValueError(
            f"ai 视图配置 schema 不匹配: config={config.target_schema}, database={expected_schema}"
        )

    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {config.target_schema}"))

        for view in config.views:
            sql = _build_view_sql(target_schema=config.target_schema, definition=view)
            await conn.execute(text(sql))

            source_relation, relation_lookup = _build_relation_lookup(view)
            relation_comments = await _load_relation_comments(conn, relation_lookup)

            if view.description:
                escaped_desc = view.description.replace("'", "''")
                comment_sql = f"COMMENT ON VIEW {config.target_schema}.{view.name} IS '{escaped_desc}'"
                await conn.execute(text(comment_sql))

            for col_expr in view.columns:
                col_name = _extract_output_column_name(col_expr)
                col_comment = view.column_comments.get(col_name)
                if not col_comment:
                    col_comment = _infer_column_comment_from_relations(
                        column_expr=col_expr,
                        source_relation=source_relation,
                        relation_lookup=relation_lookup,
                        relation_comments=relation_comments,
                    )
                if col_comment:
                    escaped = col_comment.replace("'", "''")
                    col_comment_sql = (
                        f"COMMENT ON COLUMN {config.target_schema}.{view.name}.{col_name} IS '{escaped}'"
                    )
                    await conn.execute(text(col_comment_sql))

            logger.info(
                "ai view 已同步: schema=%s, view=%s, columns=%s, joins=%s, filters=%s",
                config.target_schema,
                view.name,
                len(view.columns),
                len(view.joins),
                len(view.filters),
            )

        owner_role = str((await conn.execute(text("SELECT CURRENT_USER"))).scalar_one())
        grant_sqls = _build_grant_sqls(
            target_schema=config.target_schema,
            owner_role=owner_role,
            grantee_roles=grantee_roles or [],
        )
        for grant_sql in grant_sqls:
            await conn.execute(text(grant_sql))

        if grant_sqls:
            logger.info(
                "ai view 权限已同步: schema=%s, owner=%s, grantees=%s",
                config.target_schema,
                owner_role,
                ",".join(grantee_roles or []),
            )

    logger.info("ai view 同步完成: schema=%s, total=%s", config.target_schema, len(config.views))
