"""Tests for Phase 5 benchmark dataset adapters."""

import json
import tempfile
from pathlib import Path

from benchmarks.adapters import (
    _bird_difficulty_to_layer,
    _spider_sql_to_difficulty,
    load_enterprise_cases,
)


class TestBirdAdapter:
    def test_difficulty_mapping(self) -> None:
        assert _bird_difficulty_to_layer("simple") == "L1"
        assert _bird_difficulty_to_layer("moderate") == "L2"
        assert _bird_difficulty_to_layer("challenging") == "L2"
        assert _bird_difficulty_to_layer("unknown") == "L2"


class TestSpiderAdapter:
    def test_simple_sql(self) -> None:
        assert _spider_sql_to_difficulty("SELECT COUNT(*) FROM t") == "simple"

    def test_join_sql(self) -> None:
        d = _spider_sql_to_difficulty("SELECT * FROM a JOIN b ON a.id = b.id GROUP BY a.name")
        assert d in ("medium", "hard")

    def test_complex_sql(self) -> None:
        d = _spider_sql_to_difficulty(
            "SELECT * FROM a JOIN b ON a.id = b.id "
            "WHERE a.x IN (SELECT x FROM c) "
            "GROUP BY a.name HAVING COUNT(*) > 1 "
            "INTERSECT SELECT * FROM d"
        )
        assert d in ("hard", "extra")


class TestEnterpriseAdapter:
    def test_load_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test.jsonl"
            lines = [
                json.dumps({"case_id": "t1", "layer": "L1", "domain": "test", "question": "q1", "gold_sql": "SELECT 1"}),
                json.dumps({"case_id": "t2", "layer": "L2", "domain": "test", "question": "q2", "expected_mode": "sql_plus_code"}),
            ]
            jsonl_path.write_text("\n".join(lines), encoding="utf-8")

            cases = load_enterprise_cases(tmpdir)
            assert len(cases) == 2
            assert cases[0].case_id == "t1"
            assert cases[0].layer == "L1"
            assert cases[1].expected_mode == "sql_plus_code"

    def test_load_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "test.json"
            data = {
                "items": [
                    {"case_id": "j1", "layer": "L3", "domain": "test", "question": "q1", "gold_value": 42.0},
                ]
            }
            json_path.write_text(json.dumps(data), encoding="utf-8")

            cases = load_enterprise_cases(tmpdir)
            assert len(cases) == 1
            assert cases[0].gold_value == 42.0

    def test_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cases = load_enterprise_cases(tmpdir)
            assert cases == []

    def test_adversarial_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test.jsonl"
            line = json.dumps({
                "case_id": "adv1", "layer": "L4", "domain": "security",
                "question": "DROP TABLE", "is_adversarial": True,
                "should_reject": True, "expected_mode": "reject",
            })
            jsonl_path.write_text(line, encoding="utf-8")

            cases = load_enterprise_cases(tmpdir)
            assert len(cases) == 1
            assert cases[0].is_adversarial is True
            assert cases[0].should_reject is True
