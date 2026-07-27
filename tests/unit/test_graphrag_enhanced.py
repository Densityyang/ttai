"""Tests for enhanced GraphRAG -- caching, early stop, keyword query."""


from src.nl2sql.infra.store.graph_rag import SchemaRelationGraph, _extract_simple_column_name


class FakeView:
    """Minimal ViewDefinition-like object for testing."""
    def __init__(self, name, source_table, description="", joins=None, columns=None, column_comments=None):
        self.name = name
        self.source_table = source_table
        self.description = description
        self.joins = joins or []
        self.columns = columns or []
        self.column_comments = column_comments or {}


class FakeJoin:
    def __init__(self, table, type_="left", join_condition=""):
        self.table = table
        self.type = type_
        self.join_condition = join_condition


class FakeConfig:
    """Minimal AIViewsConfig-like object."""
    def __init__(self, views):
        self.views = views


def _make_graph() -> SchemaRelationGraph:
    config = FakeConfig(views=[
        FakeView(
            name="v_orders",
            source_table="orders",
            description="投诉工单视图",
            joins=[FakeJoin("dim_area", "left", "orders.area_id = dim_area.id")],
            columns=["id", "area_id", "status", "created_at"],
            column_comments={"area_id": "区县ID", "status": "工单状态"},
        ),
        FakeView(
            name="v_metrics",
            source_table="metrics",
            description="指标汇总视图",
            columns=["metric_name", "metric_value"],
            column_comments={"metric_name": "指标名称"},
        ),
    ])
    return SchemaRelationGraph(config)


def test_expand_with_caching() -> None:
    graph = _make_graph()
    r1 = graph.expand_tables(["v_orders"], max_hops=1)
    r2 = graph.expand_tables(["v_orders"], max_hops=1)
    assert r1 == r2
    assert len(graph._expansion_cache) >= 1


def test_expand_early_stop() -> None:
    """When no new nodes are found, expansion should stop early."""
    graph = _make_graph()
    result = graph.expand_tables(["v_orders"], max_hops=10)
    assert isinstance(result["expanded_tables"], list)


def test_query_by_keywords() -> None:
    graph = _make_graph()
    matches = graph.query_by_keywords(["投诉", "工单"])
    assert "v_orders" in matches


def test_query_by_keywords_no_match() -> None:
    graph = _make_graph()
    matches = graph.query_by_keywords(["不存在的关键词xyz"])
    assert matches == []


def test_get_column_comments() -> None:
    graph = _make_graph()
    comments = graph.get_column_comments("v_orders")
    assert "area_id" in comments
    assert comments["area_id"] == "区县ID"


def test_expand_empty_seeds() -> None:
    graph = _make_graph()
    result = graph.expand_tables([], max_hops=2)
    assert result["expanded_tables"] == []


def test_extract_simple_column_name() -> None:
    assert _extract_simple_column_name("t.column_name AS alias") == "alias"
    assert _extract_simple_column_name("table.col") == "col"
    assert _extract_simple_column_name("simple_col") == "simple_col"
    assert _extract_simple_column_name("") is None
