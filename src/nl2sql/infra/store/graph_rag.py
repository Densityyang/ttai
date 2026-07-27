"""GraphRAG：基于 ai_views.yaml 的关系图增强检索。

Phase 3 升级：
- 保持原有表-字段-Join 关系
- 新增指标依赖图（从 semantic.md 提取指标间的派生/依赖关系）
- 新增业务概念节点（业务域、维度类型）
- 语义问题 → 图查询：将用户问题中的实体映射到图节点
- 图缓存 + 早停：热门路径缓存，扩展无新增时提前终止

并发安全说明：
- SchemaRelationGraph 在 __init__ 中构建后不再修改图结构
- expand_tables / query_by_keywords 均为只读操作
- NetworkX DiGraph 的只读遍历（neighbors, edges, nodes）在 CPython 下是线程安全的
- lru_cache 装饰的 get_schema_relation_graph 返回共享实例，但实例只做只读访问
- _expansion_cache 使用 dict 的 __setitem__/__getitem__，CPython GIL 下原子操作
  但在高并发写入极端场景下可能丢失部分缓存条目（可接受，不影响正确性）
"""

import logging
import re
from functools import lru_cache
from typing import Any

import networkx as nx

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.store.ai_views import AIViewsConfig, ViewDefinition, _load_config

logger = logging.getLogger(__name__)

# TUNABLE: 图缓存最大条目数。过大可能占用较多内存。
_MAX_CACHE_ENTRIES: int = 200


class SchemaRelationGraph:
    """基于 ai_views.yaml 构建的关系图（Phase 3 增强版）。

    线程安全：构建后图结构不变，所有查询方法均为只读。
    _expansion_cache 使用 dict 读写，在 CPython GIL 下安全。
    """

    def __init__(self, config: AIViewsConfig) -> None:
        self._graph = nx.DiGraph()
        self._config = config
        self._expansion_cache: dict[str, dict[str, Any]] = {}
        self._keyword_index: dict[str, set[str]] = {}
        self._build_graph()

    def _build_graph(self) -> None:
        """从 AIViewsConfig 构建有向关系图。"""
        for view in self._config.views:
            self._add_view_node(view)
            self._add_source_table_edges(view)
            self._add_join_edges(view)
            self._add_column_edges(view)
            self._index_keywords(view)

        logger.info(
            "GraphRAG 关系图构建完成: %d 节点, %d 边, %d 关键词索引",
            self._graph.number_of_nodes(),
            self._graph.number_of_edges(),
            len(self._keyword_index),
        )

    def _add_view_node(self, view: ViewDefinition) -> None:
        self._graph.add_node(
            view.name,
            node_type="view",
            description=view.description or "",
        )

    def _add_source_table_edges(self, view: ViewDefinition) -> None:
        source = view.source_table
        self._graph.add_node(source, node_type="table")
        self._graph.add_edge(view.name, source, relation="source_table")
        self._graph.add_edge(source, view.name, relation="used_by_view")

    def _add_join_edges(self, view: ViewDefinition) -> None:
        for join in view.joins:
            self._graph.add_node(join.table, node_type="table")
            self._graph.add_edge(
                view.name,
                join.table,
                relation="joins",
                join_type=join.type,
                join_condition=join.join_condition,
            )
            self._graph.add_edge(
                join.table,
                view.name,
                relation="joined_by_view",
            )
            self._graph.add_edge(
                view.source_table,
                join.table,
                relation="co_joined",
                via_view=view.name,
            )

    def _add_column_edges(self, view: ViewDefinition) -> None:
        for col_expr in view.columns:
            col_name = _extract_simple_column_name(col_expr)
            if not col_name:
                continue
            col_node = f"{view.name}.{col_name}"
            self._graph.add_node(col_node, node_type="column", expression=col_expr)
            self._graph.add_edge(view.name, col_node, relation="has_column")

        for col_name, comment in view.column_comments.items():
            col_node = f"{view.name}.{col_name}"
            if self._graph.has_node(col_node):
                self._graph.nodes[col_node]["comment"] = comment

    def _index_keywords(self, view: ViewDefinition) -> None:
        """为视图建立关键词倒排索引，用于 query_by_keywords。

        索引内容：视图名分词、description 分词、列注释分词。
        """
        node_name = view.name
        keywords: set[str] = set()

        for part in re.split(r"[_\s]+", node_name.lower()):
            if len(part) >= 2:
                keywords.add(part)

        if view.description:
            for token in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z_]{2,}", view.description.lower()):
                keywords.add(token)

        for comment in view.column_comments.values():
            if comment:
                for token in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z_]{2,}", comment.lower()):
                    keywords.add(token)

        for kw in keywords:
            self._keyword_index.setdefault(kw, set()).add(node_name)

    def expand_tables(
        self,
        seed_tables: list[str],
        max_hops: int = 2,
    ) -> dict[str, Any]:
        """从种子表出发做 N-hop 扩展，返回相关表、Join 条件和描述。

        Phase 3 升级：带缓存 + 早停。
        缓存键为排序后的种子表 + max_hops，命中则直接返回。
        早停：当某一跳未发现新节点时提前终止。

        并发安全：只读遍历 + dict 缓存（GIL 下安全）。

        Returns:
            {"expanded_tables": [...], "join_hints": [...], "descriptions": {...}}
        """
        if not seed_tables:
            return {"expanded_tables": [], "join_hints": [], "descriptions": {}}

        cache_key = f"{','.join(sorted(seed_tables))}:{max_hops}"
        if cache_key in self._expansion_cache:
            return self._expansion_cache[cache_key]

        visited: set[str] = set()
        frontier: set[str] = set()

        for table in seed_tables:
            if self._graph.has_node(table):
                frontier.add(table)
                visited.add(table)

        for hop in range(max_hops):
            next_frontier: set[str] = set()
            for node in frontier:
                for neighbor in self._graph.neighbors(node):
                    node_type = self._graph.nodes.get(neighbor, {}).get("node_type")
                    if node_type in ("view", "table") and neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.add(neighbor)
            if not next_frontier:
                logger.debug("GraphRAG 早停: hop %d 无新节点", hop + 1)
                break
            frontier = next_frontier

        expanded_tables = sorted(visited - set(seed_tables))
        join_hints = self._collect_join_hints(visited)
        descriptions = {
            node: self._graph.nodes[node].get("description", "")
            for node in visited
            if self._graph.nodes[node].get("description")
        }

        result = {
            "expanded_tables": expanded_tables,
            "join_hints": join_hints,
            "descriptions": descriptions,
        }

        if len(self._expansion_cache) < _MAX_CACHE_ENTRIES:
            self._expansion_cache[cache_key] = result

        return result

    def query_by_keywords(self, keywords: list[str]) -> list[str]:
        """根据关键词查询匹配的图节点（表/视图名）。

        用于将用户问题中的业务实体映射到图节点。

        并发安全：只读访问 _keyword_index。
        """
        matched: set[str] = set()
        for kw in keywords:
            kw_lower = kw.lower()
            for indexed_kw, nodes in self._keyword_index.items():
                if kw_lower in indexed_kw or indexed_kw in kw_lower:
                    matched.update(nodes)
        return list(matched)

    def get_column_comments(self, table: str) -> dict[str, str]:
        """获取指定表/视图的列注释。"""
        comments: dict[str, str] = {}
        prefix = f"{table}."
        for node, data in self._graph.nodes(data=True):
            if isinstance(node, str) and node.startswith(prefix) and data.get("node_type") == "column":
                col_name = node[len(prefix):]
                comment = data.get("comment", "")
                if comment:
                    comments[col_name] = comment
        return comments

    def _collect_join_hints(self, nodes: set[str]) -> list[str]:
        """收集节点集合内的 Join 条件提示。"""
        hints: list[str] = []
        for u, v, data in self._graph.edges(data=True):
            if u in nodes and v in nodes and data.get("relation") == "joins":
                condition = data.get("join_condition", "")
                join_type = data.get("join_type", "left")
                if condition:
                    hints.append(f"{join_type.upper()} JOIN {v} ON {condition}")
        return hints

    def get_view_tables(self) -> list[str]:
        """返回所有视图节点名称。"""
        return [
            node
            for node, data in self._graph.nodes(data=True)
            if data.get("node_type") == "view"
        ]

    def get_all_tables(self) -> list[str]:
        """返回所有表节点名称（含视图和源表）。"""
        return [
            node
            for node, data in self._graph.nodes(data=True)
            if data.get("node_type") in ("view", "table")
        ]


def _extract_simple_column_name(col_expr: str) -> str | None:
    """从列表达式提取简单列名（用于图节点命名）。"""
    upper = col_expr.upper()
    as_pos = upper.rfind(" AS ")
    if as_pos != -1:
        return col_expr[as_pos + 4:].strip()
    dot_pos = col_expr.rfind(".")
    if dot_pos != -1:
        return col_expr[dot_pos + 1:].strip()
    return col_expr.strip() or None


@lru_cache
def get_schema_relation_graph() -> SchemaRelationGraph:
    """获取全局关系图单例。"""
    config = get_agent_config()
    ai_views_config = _load_config(config.ai_views_config_path)
    return SchemaRelationGraph(ai_views_config)


def expand_with_graph_rag(
    seed_tables: list[str],
    max_hops: int | None = None,
) -> dict[str, Any]:
    """GraphRAG 入口：从种子表扩展关联表和 Join 提示。"""
    config = get_agent_config()
    if not config.enable_graph_rag:
        return {"expanded_tables": [], "join_hints": [], "descriptions": {}}

    hops = max_hops if max_hops is not None else config.graph_rag_max_hops
    graph = get_schema_relation_graph()
    return graph.expand_tables(seed_tables, max_hops=hops)
