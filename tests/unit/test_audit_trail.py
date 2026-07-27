"""Tests for Phase 4 audit trail."""

from src.nl2sql.infra.observer.audit_trail import AuditEvent, create_audit_trail


class TestAuditTrail:
    def test_create_with_defaults(self) -> None:
        trail = create_audit_trail()
        assert trail.trace_id
        assert trail.events == []

    def test_record_event(self) -> None:
        trail = create_audit_trail(question="test question")
        trail.record("routing", "decision", route="pathA", reason="simple")
        assert len(trail.events) == 1
        assert trail.events[0].stage == "routing"
        assert trail.events[0].data["route"] == "pathA"

    def test_record_routing(self) -> None:
        trail = create_audit_trail()
        trail.record_routing("fast", "简单查询", signals={"entity_count": 1})
        assert trail.events[0].event_type == "decision"
        assert trail.events[0].data["signals"]["entity_count"] == 1

    def test_record_rag(self) -> None:
        trail = create_audit_trail()
        trail.record_rag(
            confidence_tier="correct",
            avg_score=0.85,
            evidence_count=3,
            route_path="standard",
        )
        evt = trail.events[0]
        assert evt.data["confidence_tier"] == "correct"
        assert evt.data["avg_score"] == 0.85

    def test_record_sql_generation(self) -> None:
        trail = create_audit_trail()
        trail.record_sql_generation("SELECT * FROM orders", strategy="parallel")
        assert "SELECT" in trail.events[0].data["sql"]

    def test_record_sql_execution(self) -> None:
        trail = create_audit_trail()
        trail.record_sql_execution("SELECT 1", success=True, row_count=10)
        assert trail.events[0].data["success"] is True
        assert trail.events[0].data["row_count"] == 10

    def test_record_sql_repair(self) -> None:
        trail = create_audit_trail()
        trail.record_sql_repair("bad sql", "fixed sql", "column not found", round_num=1)
        assert trail.events[0].data["round"] == 1

    def test_record_hitl(self) -> None:
        trail = create_audit_trail()
        trail.record_hitl("confirm", plan_version=2)
        assert trail.events[0].data["plan_version"] == 2

    def test_record_codeact(self) -> None:
        trail = create_audit_trail()
        trail.record_codeact("print(1)", success=True, elapsed_ms=150.0)
        assert trail.events[0].data["elapsed_ms"] == 150.0

    def test_record_validation(self) -> None:
        trail = create_audit_trail()
        trail.record_validation(passed=True, checks={"null_check": "pass"})
        assert trail.events[0].data["passed"] is True

    def test_record_final_output(self) -> None:
        trail = create_audit_trail()
        trail.record_final_output("table", output_preview="col1|col2")
        assert trail.events[0].data["output_type"] == "table"
        assert "total_elapsed_seconds" in trail.events[0].data

    def test_to_dict(self) -> None:
        trail = create_audit_trail(thread_id="t1", user_id="u1", question="test")
        trail.record("stage1", "type1", key="val")
        d = trail.to_dict()
        assert d["thread_id"] == "t1"
        assert d["user_id"] == "u1"
        assert len(d["events"]) == 1
        assert d["events"][0]["data"]["key"] == "val"

    def test_multiple_events_ordered(self) -> None:
        trail = create_audit_trail()
        trail.record_routing("standard", "reason1")
        trail.record_rag("correct", 0.8, 5)
        trail.record_sql_generation("SELECT 1")
        trail.record_final_output("text")
        assert len(trail.events) == 4
        assert trail.events[0].stage == "routing"
        assert trail.events[-1].stage == "output"

    def test_trace_id_unique(self) -> None:
        t1 = create_audit_trail()
        t2 = create_audit_trail()
        assert t1.trace_id != t2.trace_id


class TestAuditEvent:
    def test_event_has_timestamp(self) -> None:
        evt = AuditEvent(stage="test", event_type="check", data={"x": 1})
        assert evt.timestamp > 0
