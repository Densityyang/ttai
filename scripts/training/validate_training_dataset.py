import argparse
import csv
import json
from pathlib import Path


ALLOWED_SPLIT = {"train", "val", "test"}
ALLOWED_SAMPLE_TYPE = {"sft", "preference", "trajectory"}
ALLOWED_MODE = {"sql_only", "sql_plus_code", "reject"}


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except Exception as exc:
                raise ValueError(f"{path.name}:{line_no} 非法 JSON: {exc}") from exc
    return rows


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def validate_sft(rows: list[dict]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        sid = str(row.get("sample_id", "")).strip()
        if not sid:
            errors.append("sft: sample_id 不能为空")
            continue
        input_obj = row.get("input", {})
        target_obj = row.get("target", {})
        metadata = row.get("metadata", {})
        safety = row.get("safety", {})
        if not isinstance(input_obj, dict) or "user_query" not in input_obj:
            errors.append(f"{sid}: input.user_query 缺失")
        if not isinstance(target_obj, dict) or "sql" not in target_obj:
            errors.append(f"{sid}: target.sql 缺失")
        mode = safety.get("expected_mode")
        if mode not in ALLOWED_MODE:
            errors.append(f"{sid}: safety.expected_mode 非法")
        split = metadata.get("split")
        if split not in ALLOWED_SPLIT:
            errors.append(f"{sid}: metadata.split 非法")
    return errors


def validate_preference(rows: list[dict]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        pid = str(row.get("pair_id", "")).strip()
        if not pid:
            errors.append("preference: pair_id 不能为空")
            continue
        chosen = row.get("chosen", {})
        rejected = row.get("rejected", {})
        metadata = row.get("metadata", {})
        if not isinstance(chosen, dict) or not chosen.get("sql"):
            errors.append(f"{pid}: chosen.sql 缺失")
        if not isinstance(rejected, dict) or not rejected.get("sql"):
            errors.append(f"{pid}: rejected.sql 缺失")
        split = metadata.get("split")
        if split not in ALLOWED_SPLIT:
            errors.append(f"{pid}: metadata.split 非法")
    return errors


def validate_trajectory(rows: list[dict]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        tid = str(row.get("trajectory_id", "")).strip()
        if not tid:
            errors.append("trajectory: trajectory_id 不能为空")
            continue
        steps = row.get("steps", [])
        metadata = row.get("metadata", {})
        if not isinstance(steps, list) or not steps:
            errors.append(f"{tid}: steps 不能为空")
        split = metadata.get("split")
        if split not in ALLOWED_SPLIT:
            errors.append(f"{tid}: metadata.split 非法")
    return errors


def validate_manifest(rows: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    required = {
        "sample_id",
        "sample_type",
        "source_case_id",
        "split",
        "domain",
        "layer",
        "difficulty",
        "schema_version",
        "version",
        "notes",
    }
    if not rows:
        return ["split_manifest: 空文件"]
    missing = required - set(rows[0].keys())
    if missing:
        return [f"split_manifest: 缺失列 {', '.join(sorted(missing))}"]
    for row in rows:
        sid = row.get("sample_id", "").strip()
        stype = row.get("sample_type", "").strip()
        split = row.get("split", "").strip()
        if not sid:
            errors.append("split_manifest: sample_id 不能为空")
        if stype not in ALLOWED_SAMPLE_TYPE:
            errors.append(f"{sid}: sample_type 非法")
        if split not in ALLOWED_SPLIT:
            errors.append(f"{sid}: split 非法")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sft", type=Path, required=True)
    parser.add_argument("--preference", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    try:
        sft_rows = load_jsonl(args.sft)
        pref_rows = load_jsonl(args.preference)
        traj_rows = load_jsonl(args.trajectory)
    except ValueError as exc:
        print("VALIDATION_FAILED")
        print(str(exc))
        return 1

    manifest_rows = load_csv(args.manifest)

    errors: list[str] = []
    errors.extend(validate_sft(sft_rows))
    errors.extend(validate_preference(pref_rows))
    errors.extend(validate_trajectory(traj_rows))
    errors.extend(validate_manifest(manifest_rows))

    if errors:
        print("VALIDATION_FAILED")
        for e in errors:
            print(e)
        return 1

    print("VALIDATION_PASSED")
    print(
        f"sft={len(sft_rows)} preference={len(pref_rows)} "
        f"trajectory={len(traj_rows)} manifest={len(manifest_rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
