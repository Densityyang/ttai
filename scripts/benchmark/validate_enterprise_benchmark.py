import argparse
import csv
import json
from pathlib import Path


ALLOWED_SOURCE_TYPE = {"real", "synthetic", "adversarial"}
ALLOWED_LAYER = {"L1", "L2", "L3", "L4"}
ALLOWED_MODE = {"sql_only", "sql_plus_code", "reject"}
REQUIRED_CASE_COLUMNS = {
    "case_id",
    "source_type",
    "layer",
    "domain",
    "user_query",
    "query_paraphrases",
    "risk_level",
    "permission_profile",
    "expected_mode",
    "tags",
}
REQUIRED_EXEC_COLUMNS = {
    "case_id",
    "gold_sql",
    "gold_sql_result_hash",
    "gold_calc_code",
    "gold_calc_steps_json",
    "gold_final_value_json",
    "tolerance_rule_json",
}


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def must_have_columns(rows: list[dict[str, str]], required: set[str], file_name: str) -> list[str]:
    if not rows:
        return [f"{file_name}: 空文件"]
    cols = set(rows[0].keys())
    missing = sorted(required - cols)
    if not missing:
        return []
    return [f"{file_name}: 缺失列 {', '.join(missing)}"]


def parse_json_field(value: str, field_name: str, case_id: str) -> list[str]:
    try:
        json.loads(value)
        return []
    except Exception:
        return [f"{case_id}: {field_name} 不是合法 JSON"]


def validate_cases(rows: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for row in rows:
        case_id = row.get("case_id", "").strip()
        if not case_id:
            errors.append("case_id 不能为空")
            continue
        if case_id in seen:
            errors.append(f"{case_id}: case_id 重复")
        seen.add(case_id)
        source_type = row.get("source_type", "").strip()
        if source_type not in ALLOWED_SOURCE_TYPE:
            errors.append(f"{case_id}: source_type 非法")
        layer = row.get("layer", "").strip()
        if layer not in ALLOWED_LAYER:
            errors.append(f"{case_id}: layer 非法")
        mode = row.get("expected_mode", "").strip()
        if mode not in ALLOWED_MODE:
            errors.append(f"{case_id}: expected_mode 非法")
        query = row.get("user_query", "").strip()
        if not query:
            errors.append(f"{case_id}: user_query 不能为空")
        para = row.get("query_paraphrases", "").strip()
        errors.extend(parse_json_field(para, "query_paraphrases", case_id))
        tags = row.get("tags", "").strip()
        errors.extend(parse_json_field(tags, "tags", case_id))
    return errors


def validate_exec(rows: list[dict[str, str]], case_modes: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        case_id = row.get("case_id", "").strip()
        if not case_id:
            errors.append("gold_execution_specs: case_id 不能为空")
            continue
        if case_id not in case_modes:
            errors.append(f"{case_id}: 在 benchmark_cases 中不存在")
            continue
        mode = case_modes[case_id]
        gold_sql = row.get("gold_sql", "").strip()
        gold_calc_code = row.get("gold_calc_code", "").strip()
        if mode == "sql_only":
            if not gold_sql:
                errors.append(f"{case_id}: sql_only 必须提供 gold_sql")
        elif mode == "sql_plus_code":
            if not gold_sql:
                errors.append(f"{case_id}: sql_plus_code 必须提供 gold_sql")
            if not gold_calc_code:
                errors.append(f"{case_id}: sql_plus_code 必须提供 gold_calc_code")
        elif mode == "reject":
            if gold_sql or gold_calc_code:
                errors.append(f"{case_id}: reject 模式不应提供 gold_sql 或 gold_calc_code")
        for field in ("gold_calc_steps_json", "gold_final_value_json", "tolerance_rule_json"):
            value = row.get(field, "").strip()
            if value:
                errors.extend(parse_json_field(value, field, case_id))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--exec", dest="exec_file", type=Path, required=True)
    args = parser.parse_args()

    case_rows = load_csv(args.cases)
    exec_rows = load_csv(args.exec_file)

    errors: list[str] = []
    errors.extend(must_have_columns(case_rows, REQUIRED_CASE_COLUMNS, args.cases.name))
    errors.extend(must_have_columns(exec_rows, REQUIRED_EXEC_COLUMNS, args.exec_file.name))
    if not errors:
        errors.extend(validate_cases(case_rows))
        case_modes = {r["case_id"].strip(): r["expected_mode"].strip() for r in case_rows if r.get("case_id")}
        errors.extend(validate_exec(exec_rows, case_modes))

    if errors:
        print("VALIDATION_FAILED")
        for e in errors:
            print(e)
        return 1

    print("VALIDATION_PASSED")
    print(f"cases={len(case_rows)} exec={len(exec_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
