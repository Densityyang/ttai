"""Langfuse 最小评测函数库（代码内直接调用，不依赖 CLI）。

推荐从以下函数启动：
1. run_single_case_eval: 单题评测（创建数据集样本 + 执行 + 自动打分）
2. run_single_case_eval_with_custom_rules: 支持自定义关键词/禁止词/耗时阈值
3. prepare_dataset_case_only: 仅准备数据集样本，不执行实验
4. prepare_dataset_from_json: 从 JSON 批量导入 dataset
5. run_dataset_eval: 运行整个 dataset（支持并行）
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from langfuse import Evaluation, Langfuse

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _get_langfuse_client_from_env_file() -> Langfuse:
    """从 .env 读取配置并初始化 Langfuse 客户端。"""
    from src.core.settings import get_settings

    settings = get_settings()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        raise RuntimeError(
            "缺少 Langfuse 配置，请在 .env 或 .env.<ENV> 中设置 "
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST"
        )

    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        timeout=settings.langfuse_timeout,
    )
    if not client.auth_check():
        raise RuntimeError(
            "Langfuse 鉴权失败，请检查 .env 中的 "
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST"
        )
    return client


@dataclass(frozen=True)
class EvalCase:
    """单条评测样本定义。"""

    case_id: str
    question: str
    expected_keywords_any: list[str]
    forbidden_keywords: list[str]
    max_latency_ms: float | None = None


@dataclass(frozen=True)
class EvalRunSummary:
    """实验运行结果摘要。"""

    run_name: str
    dataset_run_id: str | None
    dataset_run_url: str | None
    result_text: str
    compact_summary_text: str


@dataclass(frozen=True)
class HumanReadableEvalSummary:
    """逐条可读评测摘要。"""

    total_items: int
    passed_items: int
    failed_items: int
    avg_latency_ms: float
    summary_text: str


def _validate_max_concurrency(max_concurrency: int) -> int:
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须 >= 1")
    return max_concurrency


def _ensure_dataset(langfuse_client: Any, dataset_name: str) -> Any:
    try:
        return langfuse_client.get_dataset(dataset_name)
    except Exception:
        langfuse_client.create_dataset(
            name=dataset_name,
            description="tt-ai NL2SQL 最小评测集（函数调用版）",
            metadata={"project": "tt-ai", "type": "minimal-function-api"},
        )
        return langfuse_client.get_dataset(dataset_name)


def _upsert_case(langfuse_client: Any, dataset: Any, case: EvalCase) -> None:
    for item in dataset.items:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        if metadata.get("case_id") == case.case_id:
            return

    langfuse_client.create_dataset_item(
        dataset_name=dataset.name,
        input={"question": case.question},
        expected_output={
            "expected_keywords_any": case.expected_keywords_any,
            "forbidden_keywords": case.forbidden_keywords,
            "max_latency_ms": case.max_latency_ms,
        },
        metadata={"case_id": case.case_id, "source": "function-api"},
    )


def prepare_dataset_case_only(dataset_name: str, case: EvalCase) -> str:
    """仅准备 dataset 样本，不执行实验。返回 dataset id。"""
    langfuse_client = _get_langfuse_client_from_env_file()
    dataset = _ensure_dataset(langfuse_client=langfuse_client, dataset_name=dataset_name)
    _upsert_case(langfuse_client=langfuse_client, dataset=dataset, case=case)
    refreshed = langfuse_client.get_dataset(dataset_name)
    return str(refreshed.id)


def _parse_case_from_json_item(raw_item: Any) -> EvalCase:
    if not isinstance(raw_item, dict):
        raise ValueError("dataset item 必须是 dict")

    raw_input = raw_item.get("input")
    question = ""
    if isinstance(raw_input, dict):
        question = str(raw_input.get("question", "")).strip()
    elif isinstance(raw_input, str):
        question = raw_input.strip()

    if not question:
        raise ValueError("dataset item 缺少 input.question")

    raw_expected = raw_item.get("expected_output")
    expected_output = raw_expected if isinstance(raw_expected, dict) else {}

    case_id = str(raw_item.get("case_id", "")).strip()
    if not case_id and isinstance(raw_item.get("metadata"), dict):
        case_id = str(raw_item["metadata"].get("case_id", "")).strip()
    if not case_id:
        raise ValueError("dataset item 缺少 case_id")

    max_latency_ms: float | None = None
    raw_latency = expected_output.get("max_latency_ms")
    if isinstance(raw_latency, int | float):
        max_latency_ms = float(raw_latency)

    return EvalCase(
        case_id=case_id,
        question=question,
        expected_keywords_any=_to_str_list(expected_output.get("expected_keywords_any")),
        forbidden_keywords=_to_str_list(expected_output.get("forbidden_keywords")),
        max_latency_ms=max_latency_ms,
    )


def _extract_expected_output_from_json_item(raw_item: Any) -> dict[str, Any]:
    if not isinstance(raw_item, dict):
        return {}
    expected = raw_item.get("expected_output")
    return expected if isinstance(expected, dict) else {}


def prepare_dataset_from_json(dataset_name: str, dataset_json_path: str) -> int:
    """从 JSON 批量同步 dataset 样本，返回同步后样本总数。"""
    langfuse_client = _get_langfuse_client_from_env_file()
    dataset = _ensure_dataset(langfuse_client=langfuse_client, dataset_name=dataset_name)

    payload = json.loads(Path(dataset_json_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("JSON 顶层必须是对象")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("JSON 缺少 items 列表")

    existing_case_ids = {
        str(item.metadata.get("case_id"))
        for item in dataset.items
        if isinstance(item.metadata, dict) and item.metadata.get("case_id") is not None
    }

    for raw_item in raw_items:
        case = _parse_case_from_json_item(raw_item)
        if case.case_id in existing_case_ids:
            continue
        raw_expected = _extract_expected_output_from_json_item(raw_item)
        teacher_sql = str(raw_expected.get("teacher_sql", "")).strip()
        teacher_answer = str(raw_expected.get("teacher_answer", "")).strip()
        expected_output: dict[str, Any] = {
            "expected_keywords_any": case.expected_keywords_any,
            "forbidden_keywords": case.forbidden_keywords,
            "max_latency_ms": case.max_latency_ms,
        }
        if teacher_sql:
            expected_output["teacher_sql"] = teacher_sql
        if teacher_answer:
            expected_output["teacher_answer"] = teacher_answer
        langfuse_client.create_dataset_item(
            dataset_name=dataset.name,
            input={"question": case.question},
            expected_output=expected_output,
            metadata={"case_id": case.case_id, "source": "json-import"},
        )
        existing_case_ids.add(case.case_id)

    refreshed = langfuse_client.get_dataset(dataset_name)
    return len(refreshed.items)


def _extract_text_content(message_content: Any) -> str:
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        chunks: list[str] = []
        for part in message_content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks).strip()
    return str(message_content)


async def _collect_agent_answer(question: str, session_id: str) -> str:
    """调用 SQL Agent 并提取最终文本回答。"""
    from src.nl2sql.agents.nl2sql.service import query_database

    final_answer = ""
    async for step in query_database(question=question, session_id=session_id):
        if not isinstance(step, dict):
            continue
        messages = step.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        content = getattr(messages[-1], "content", "")
        text = _extract_text_content(content).strip()
        if text:
            final_answer = text
    return final_answer


async def _run_agent_once(
    question: str,
    session_id: str,
    *,
    suppress_agent_logs: bool = False,
) -> dict[str, Any]:
    """调用 SQL Agent 并提取输出与耗时。"""
    start = time.perf_counter()
    if suppress_agent_logs:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            final_answer = await _collect_agent_answer(question=question, session_id=session_id)
    else:
        final_answer = await _collect_agent_answer(question=question, session_id=session_id)

    latency_ms = (time.perf_counter() - start) * 1000
    return {
        "answer": final_answer,
        "latency_ms": latency_ms,
    }


async def _task(*, item: Any, **kwargs: Any) -> dict[str, Any]:
    del kwargs
    raw_input = item.input
    if isinstance(raw_input, dict):
        question = str(raw_input.get("question", "")).strip()
    else:
        question = str(raw_input).strip()
    return await _run_agent_once(question=question, session_id=f"langfuse-eval-{item.id}")


def _to_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_numeric_text(value: str) -> str | None:
    cleaned = value.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        if "." in cleaned:
            normalized = f"{float(cleaned):.8f}".rstrip("0").rstrip(".")
            return normalized if normalized else "0"
        return str(int(cleaned))
    except ValueError:
        return None


def _extract_expected_single_numeric(expected_output: Any) -> str | None:
    if not isinstance(expected_output, dict):
        return None
    teacher_answer = expected_output.get("teacher_answer")
    if not isinstance(teacher_answer, str):
        return None

    match = re.match(r"^\s*\{\s*'[^']+'\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*\}\s*$", teacher_answer)
    if match is None:
        return None
    return _normalize_numeric_text(match.group(1))


def _extract_numeric_tokens(text: str) -> set[str]:
    tokens = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    results: set[str] = set()
    for token in tokens:
        normalized = _normalize_numeric_text(token)
        if normalized is not None:
            results.add(normalized)
    return results


def _answer_semantically_means_zero(answer: str) -> bool:
    compact = answer.replace(" ", "").replace("\n", "")
    negative_hints = ["不是0", "不为0", "非0", "大于0", "超过0"]
    if any(hint in compact for hint in negative_hints):
        return False

    zero_hints = ["没有", "无", "暂无", "未发现", "不存在", "为零", "0单", "0条", "0个"]
    return any(hint in compact for hint in zero_hints)


def _is_numeric_or_zero_semantic_match(*, answer: str, expected_output: Any) -> bool:
    expected_numeric = _extract_expected_single_numeric(expected_output)
    if expected_numeric is None:
        return False

    answer_numbers = _extract_numeric_tokens(answer)
    if expected_numeric in answer_numbers:
        return True

    return expected_numeric == "0" and _answer_semantically_means_zero(answer)


def _evaluation_list_to_map(evaluations: list[Evaluation]) -> dict[str, float]:
    score_map: dict[str, float] = {}
    for evaluation in evaluations:
        score = _to_float_or_none(evaluation.value)
        if score is None:
            continue
        score_map[evaluation.name] = score
    return score_map


def _binary_eval_total(eval_map: dict[str, float]) -> tuple[int, int]:
    keys = ["keyword_any_hit", "no_forbidden_keyword", "answer_not_empty", "latency_threshold_pass"]
    available = [name for name in keys if name in eval_map]
    if not available:
        return (0, 0)
    passed = sum(1 for name in available if eval_map[name] >= 1.0)
    return (passed, len(available))


def _truncate_text(value: str, max_chars: int) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...(截断, 原始长度={len(text)})"


def _extract_case_id_from_raw_json_item(raw_item: Any, *, fallback: str) -> str:
    if isinstance(raw_item, dict):
        case_id = str(raw_item.get("case_id", "")).strip()
        if case_id:
            return case_id
        metadata = raw_item.get("metadata")
        if isinstance(metadata, dict):
            case_id = str(metadata.get("case_id", "")).strip()
            if case_id:
                return case_id
    return fallback


def _build_human_readable_eval_input_items(
    dataset_json_path: str,
    *,
    case_ids: list[str] | None,
) -> list[dict[str, Any]]:
    payload = json.loads(Path(dataset_json_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("JSON 顶层必须是对象")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("JSON 缺少 items 列表")

    if not case_ids:
        return raw_items

    case_id_set = {case_id.strip() for case_id in case_ids if case_id.strip()}
    filtered: list[dict[str, Any]] = []
    for idx, raw_item in enumerate(raw_items, start=1):
        case_id = _extract_case_id_from_raw_json_item(raw_item, fallback=f"item-{idx}")
        if case_id in case_id_set and isinstance(raw_item, dict):
            filtered.append(raw_item)
    return filtered


async def run_human_readable_eval_from_json(
    *,
    dataset_json_path: str,
    case_ids: list[str] | None = None,
    max_display_chars: int = 800,
    pause_each_item: bool = False,
) -> HumanReadableEvalSummary:
    """串行逐条评测，实时输出人类可读结果。"""
    raw_items = _build_human_readable_eval_input_items(dataset_json_path, case_ids=case_ids)
    if not raw_items:
        raise RuntimeError("没有可运行样本，请检查 JSON 文件或 case_id 过滤条件")

    total = 0
    passed = 0
    latency_values: list[float] = []
    failed_case_ids: list[str] = []

    for idx, raw_item in enumerate(raw_items, start=1):
        case = _parse_case_from_json_item(raw_item)
        expected_output = _extract_expected_output_from_json_item(raw_item)
        teacher_answer = str(expected_output.get("teacher_answer", "")).strip()
        teacher_sql = str(expected_output.get("teacher_sql", "")).strip()

        result = await _run_agent_once(
            question=case.question,
            session_id=f"human-eval-{case.case_id}-{idx}",
            suppress_agent_logs=True,
        )
        evaluations = evaluator_quality_and_latency(
            input={"question": case.question},
            output=result,
            expected_output=expected_output,
        )
        eval_map = _evaluation_list_to_map(evaluations)
        score_passed, score_total = _binary_eval_total(eval_map)
        is_passed = score_total > 0 and score_passed == score_total

        answer = str(result.get("answer", "")).strip()
        latency_ms = _to_float_or_none(result.get("latency_ms")) or 0.0
        latency_values.append(latency_ms)
        total += 1
        if is_passed:
            passed += 1
        else:
            failed_case_ids.append(case.case_id)

        print(f"\n{'=' * 90}")
        print(f"[{idx}/{len(raw_items)}] case_id={case.case_id}")
        print(f"问题: {case.question}")
        if teacher_sql:
            print(f"老师SQL: {_truncate_text(teacher_sql, max_display_chars)}")
        print(f"模型答案: {_truncate_text(answer, max_display_chars)}")
        print(
            "标准答案: "
            + (_truncate_text(teacher_answer, max_display_chars) if teacher_answer else "(未提供)")
        )
        print(f"耗时: {latency_ms:.2f} ms")
        print(
            "评分: "
            + ", ".join(
                [
                    f"keyword_any_hit={eval_map.get('keyword_any_hit', 0.0):.2f}",
                    f"no_forbidden_keyword={eval_map.get('no_forbidden_keyword', 0.0):.2f}",
                    f"answer_not_empty={eval_map.get('answer_not_empty', 0.0):.2f}",
                    f"latency_threshold_pass={eval_map.get('latency_threshold_pass', 0.0):.2f}",
                ]
            )
        )
        print(f"单条结论: {'PASS' if is_passed else 'FAIL'} ({score_passed}/{score_total})")

        if pause_each_item and idx < len(raw_items):
            try:
                user_input = input("按回车继续，输入 q 结束评测: ").strip().lower()
            except EOFError:
                user_input = ""
            if user_input == "q":
                break

    avg_latency = mean(latency_values) if latency_values else 0.0
    failed = total - passed
    lines = [
        f"total={total}",
        f"passed={passed}",
        f"failed={failed}",
        f"avg_latency_ms={avg_latency:.2f}",
    ]
    if failed_case_ids:
        lines.append("failed_case_ids=" + ", ".join(failed_case_ids))

    summary_text = "\n".join(lines)
    return HumanReadableEvalSummary(
        total_items=total,
        passed_items=passed,
        failed_items=failed,
        avg_latency_ms=avg_latency,
        summary_text=summary_text,
    )


def evaluator_quality_and_latency(
    *,
    input: Any,
    output: Any,
    expected_output: Any | None = None,
    **kwargs: Any,
) -> list[Evaluation]:
    """单条样本评估器：关键词、禁止词、非空、耗时阈值。"""
    del input, kwargs

    answer = ""
    latency_ms = 0.0
    if isinstance(output, dict):
        answer = str(output.get("answer", "")).strip()
        raw_latency = output.get("latency_ms", 0.0)
        if isinstance(raw_latency, int | float):
            latency_ms = float(raw_latency)
    else:
        answer = str(output).strip()

    expected_keywords_any: list[str] = []
    forbidden_keywords: list[str] = []
    max_latency_ms: float | None = None
    if isinstance(expected_output, dict):
        expected_keywords_any = _to_str_list(expected_output.get("expected_keywords_any"))
        forbidden_keywords = _to_str_list(expected_output.get("forbidden_keywords"))
        max_latency_raw = expected_output.get("max_latency_ms")
        if isinstance(max_latency_raw, int | float):
            max_latency_ms = float(max_latency_raw)

    hit_by_keyword = True if not expected_keywords_any else any(k in answer for k in expected_keywords_any)
    hit_by_numeric = _is_numeric_or_zero_semantic_match(answer=answer, expected_output=expected_output)
    hit_any = hit_by_keyword or hit_by_numeric
    no_forbidden = not any(k in answer for k in forbidden_keywords)
    non_empty = bool(answer)
    latency_ok = True if max_latency_ms is None else latency_ms <= max_latency_ms

    return [
        Evaluation(
            name="keyword_any_hit",
            value=1.0 if hit_any else 0.0,
            comment=f"expected_any={expected_keywords_any}",
        ),
        Evaluation(
            name="no_forbidden_keyword",
            value=1.0 if no_forbidden else 0.0,
            comment=f"forbidden={forbidden_keywords}",
        ),
        Evaluation(
            name="answer_not_empty",
            value=1.0 if non_empty else 0.0,
            comment="回答非空检查",
        ),
        Evaluation(
            name="latency_ms",
            value=latency_ms,
            comment="单条样本耗时（毫秒）",
        ),
        Evaluation(
            name="latency_threshold_pass",
            value=1.0 if latency_ok else 0.0,
            comment=(
                "未设置阈值"
                if max_latency_ms is None
                else f"threshold={max_latency_ms:.2f}ms"
            ),
        ),
    ]


def _read_output(item_result: Any) -> dict[str, Any] | None:
    if isinstance(item_result, dict):
        output = item_result.get("output")
    else:
        output = getattr(item_result, "output", None)
    return output if isinstance(output, dict) else None


def _to_float_or_none(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _extract_case_id_from_item_result(item_result: Any) -> str:
    item = getattr(item_result, "item", None)
    metadata = getattr(item, "metadata", None)
    if isinstance(metadata, dict):
        case_id = str(metadata.get("case_id", "")).strip()
        if case_id:
            return case_id
    item_id = getattr(item, "id", None)
    return str(item_id) if item_id is not None else "unknown"


def _extract_eval_map(item_result: Any) -> dict[str, float]:
    evaluations = getattr(item_result, "evaluations", None)
    if not isinstance(evaluations, list):
        return {}

    eval_map: dict[str, float] = {}
    for evaluation in evaluations:
        name = str(getattr(evaluation, "name", "")).strip()
        if not name:
            continue
        value = _to_float_or_none(getattr(evaluation, "value", None))
        if value is None:
            continue
        eval_map[name] = value
    return eval_map


def build_compact_summary(
    result: Any,
    *,
    max_failed_cases: int = 20,
) -> str:
    """构建紧凑可读摘要：总体得分 + 失败样本列表。"""
    item_results = getattr(result, "item_results", None)
    if not isinstance(item_results, list) or not item_results:
        return "无可用 item_results。"

    # 聚合每个评估维度的均值
    eval_buckets: dict[str, list[float]] = {}
    failed_cases: list[tuple[str, list[str]]] = []

    binary_checks = [
        "keyword_any_hit",
        "no_forbidden_keyword",
        "answer_not_empty",
        "latency_threshold_pass",
    ]

    for item_result in item_results:
        eval_map = _extract_eval_map(item_result)
        for name, value in eval_map.items():
            eval_buckets.setdefault(name, []).append(value)

        failed_reasons = [name for name in binary_checks if name in eval_map and eval_map[name] < 1.0]
        if failed_reasons:
            failed_cases.append((_extract_case_id_from_item_result(item_result), failed_reasons))

    lines: list[str] = []
    lines.append(f"items={len(item_results)}")
    for metric_name in sorted(eval_buckets.keys()):
        values = eval_buckets[metric_name]
        lines.append(f"avg.{metric_name}={mean(values):.4f}")

    run_evaluations = getattr(result, "run_evaluations", None)
    if isinstance(run_evaluations, list):
        for evaluation in run_evaluations:
            metric_name = str(getattr(evaluation, "name", "")).strip()
            metric_value = _to_float_or_none(getattr(evaluation, "value", None))
            if metric_name and metric_value is not None:
                lines.append(f"run.{metric_name}={metric_value:.4f}")

    lines.append(f"failed_cases={len(failed_cases)}")
    for case_id, reasons in failed_cases[:max_failed_cases]:
        lines.append(f"- {case_id}: {', '.join(reasons)}")
    if len(failed_cases) > max_failed_cases:
        lines.append(f"- ... 其余 {len(failed_cases) - max_failed_cases} 条未展示")

    return "\n".join(lines)


def run_evaluator_latency_aggregate(*, item_results: list[Any], **kwargs: Any) -> list[Evaluation]:
    """实验级耗时评估器：平均耗时 + P95。"""
    del kwargs
    latency_values: list[float] = []
    for item_result in item_results:
        output = _read_output(item_result)
        if output is None:
            continue
        raw_latency = output.get("latency_ms")
        if isinstance(raw_latency, int | float):
            latency_values.append(float(raw_latency))

    if not latency_values:
        return [
            Evaluation(
                name="run_avg_latency_ms",
                value=0.0,
                comment="无可用耗时数据",
            ),
            Evaluation(
                name="run_p95_latency_ms",
                value=0.0,
                comment="无可用耗时数据",
            ),
        ]

    sorted_values = sorted(latency_values)
    p95_index = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * 0.95) - 1))
    p95 = sorted_values[p95_index]
    avg = mean(sorted_values)
    return [
        Evaluation(
            name="run_avg_latency_ms",
            value=avg,
            comment=f"样本数={len(sorted_values)}",
        ),
        Evaluation(
            name="run_p95_latency_ms",
            value=p95,
            comment=f"样本数={len(sorted_values)}",
        ),
    ]


def _fetch_target_items(dataset: Any, case_id: str) -> list[Any]:
    return [
        item
        for item in dataset.items
        if isinstance(item.metadata, dict) and item.metadata.get("case_id") == case_id
    ]


def run_single_case_eval(
    *,
    dataset_name: str,
    case_id: str,
    question: str,
    must_include: str,
    experiment_name: str = "nl2sql-minimal-eval",
    max_concurrency: int = 1,
) -> EvalRunSummary:
    """启动函数 1：最简单单题评测（兼容你当前 must_include 诉求）。"""
    return run_single_case_eval_with_custom_rules(
        dataset_name=dataset_name,
        case=EvalCase(
            case_id=case_id,
            question=question,
            expected_keywords_any=[must_include] if must_include.strip() else [],
            forbidden_keywords=[],
            max_latency_ms=None,
        ),
        experiment_name=experiment_name,
        max_concurrency=max_concurrency,
    )


def run_single_case_eval_with_custom_rules(
    *,
    dataset_name: str,
    case: EvalCase,
    experiment_name: str = "nl2sql-minimal-eval",
    max_concurrency: int = 1,
) -> EvalRunSummary:
    """启动函数 2：支持自定义评分规则（关键词、禁止词、耗时阈值）。"""
    langfuse_client = _get_langfuse_client_from_env_file()
    dataset = _ensure_dataset(langfuse_client=langfuse_client, dataset_name=dataset_name)
    _upsert_case(langfuse_client=langfuse_client, dataset=dataset, case=case)

    dataset = langfuse_client.get_dataset(dataset_name)
    target_items = _fetch_target_items(dataset=dataset, case_id=case.case_id)
    if not target_items:
        raise RuntimeError(f"未找到 case_id={case.case_id} 的样本")

    run_name = f"{experiment_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    validated_concurrency = _validate_max_concurrency(max_concurrency)
    result = langfuse_client.run_experiment(
        name=experiment_name,
        run_name=run_name,
        description="tt-ai nl2sql 最小评测（单题，函数调用）",
        data=target_items,
        task=_task,
        evaluators=[evaluator_quality_and_latency],
        run_evaluators=[run_evaluator_latency_aggregate],
        max_concurrency=validated_concurrency,
        metadata={"script": "scripts/langfuse_eval_minimal.py", "mode": "function-call"},
    )

    result_text = result.format() if hasattr(result, "format") else str(result)
    compact_summary_text = build_compact_summary(result)
    return EvalRunSummary(
        run_name=str(result.run_name),
        dataset_run_id=str(result.dataset_run_id) if result.dataset_run_id else None,
        dataset_run_url=str(result.dataset_run_url) if result.dataset_run_url else None,
        result_text=result_text,
        compact_summary_text=compact_summary_text,
    )


def run_single_case_eval_with_latency_target(
    *,
    dataset_name: str,
    case_id: str,
    question: str,
    expected_keywords_any: list[str],
    max_latency_ms: float,
    experiment_name: str = "nl2sql-minimal-eval",
    max_concurrency: int = 1,
) -> EvalRunSummary:
    """启动函数 3：强调耗时目标的单题评测。"""
    case = EvalCase(
        case_id=case_id,
        question=question,
        expected_keywords_any=expected_keywords_any,
        forbidden_keywords=[],
        max_latency_ms=max_latency_ms,
    )
    return run_single_case_eval_with_custom_rules(
        dataset_name=dataset_name,
        case=case,
        experiment_name=experiment_name,
        max_concurrency=max_concurrency,
    )


def run_dataset_eval(
    *,
    dataset_name: str,
    experiment_name: str = "nl2sql-minimal-eval",
    max_concurrency: int = 4,
    case_ids: list[str] | None = None,
) -> EvalRunSummary:
    """运行整个 dataset（可按 case_id 过滤）。"""
    langfuse_client = _get_langfuse_client_from_env_file()
    dataset = _ensure_dataset(langfuse_client=langfuse_client, dataset_name=dataset_name)

    target_items: list[Any]
    if case_ids:
        case_id_set = {case_id.strip() for case_id in case_ids if case_id.strip()}
        target_items = [
            item
            for item in dataset.items
            if isinstance(item.metadata, dict) and str(item.metadata.get("case_id", "")).strip() in case_id_set
        ]
    else:
        target_items = list(dataset.items)

    if not target_items:
        raise RuntimeError("没有可运行的 dataset items，请先导入样本或检查 case_ids 过滤条件")

    run_name = f"{experiment_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    validated_concurrency = _validate_max_concurrency(max_concurrency)
    result = langfuse_client.run_experiment(
        name=experiment_name,
        run_name=run_name,
        description="tt-ai nl2sql 最小评测（dataset 批量）",
        data=target_items,
        task=_task,
        evaluators=[evaluator_quality_and_latency],
        run_evaluators=[run_evaluator_latency_aggregate],
        max_concurrency=validated_concurrency,
        metadata={"script": "scripts/langfuse_eval_minimal.py", "mode": "dataset-batch"},
    )

    result_text = result.format() if hasattr(result, "format") else str(result)
    compact_summary_text = build_compact_summary(result)
    return EvalRunSummary(
        run_name=str(result.run_name),
        dataset_run_id=str(result.dataset_run_id) if result.dataset_run_id else None,
        dataset_run_url=str(result.dataset_run_url) if result.dataset_run_url else None,
        result_text=result_text,
        compact_summary_text=compact_summary_text,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Langfuse 最小评测脚本")
    parser.add_argument(
        "--mode",
        choices=["single", "dataset", "dataset-json", "human-json"],
        default="single",
        help="single=单题，dataset=直接跑 Langfuse dataset，dataset-json=先导入 JSON 再跑，human-json=逐条可读评测",
    )
    parser.add_argument("--dataset-name", default="tt-ai-nl2sql-minimal", help="Langfuse dataset 名称")
    parser.add_argument(
        "--dataset-json-path",
        default="scripts/data/langfuse_teacher_dataset.json",
        help="JSON 数据集路径（mode=dataset-json 时使用）",
    )
    parser.add_argument("--experiment-name", default="nl2sql-minimal-eval", help="实验名称")
    parser.add_argument("--max-concurrency", type=int, default=1, help="并发数（>=1）")
    parser.add_argument("--summary-only", action="store_true", help="仅输出紧凑摘要")
    parser.add_argument("--max-display-chars", type=int, default=800, help="human-json 模式单字段最大展示字符数")
    parser.add_argument("--pause-each-item", action="store_true", help="human-json 模式下每条输出后暂停，回车继续")
    parser.add_argument("--case-id", default="case-001", help="single 模式 case_id")
    parser.add_argument("--question", default="查询安装工单的总数", help="single 模式问题")
    parser.add_argument("--must-include", default="工单", help="single 模式必须命中关键词")
    parser.add_argument(
        "--filter-case-id",
        action="append",
        default=[],
        help="dataset 模式按 case_id 过滤，可重复传入",
    )
    return parser.parse_args()


def main() -> None:
    """默认启动入口。"""
    args = parse_args()

    if args.mode == "human-json":
        summary = asyncio.run(
            run_human_readable_eval_from_json(
                dataset_json_path=args.dataset_json_path,
                case_ids=args.filter_case_id or None,
                max_display_chars=args.max_display_chars,
                pause_each_item=args.pause_each_item,
            )
        )
        print(f"\n{'=' * 90}")
        print("总体汇总:")
        print(summary.summary_text)
        return

    if args.mode == "dataset-json":
        total = prepare_dataset_from_json(
            dataset_name=args.dataset_name,
            dataset_json_path=args.dataset_json_path,
        )
        print(f"synced_dataset_items={total}")
        summary = run_dataset_eval(
            dataset_name=args.dataset_name,
            experiment_name=args.experiment_name,
            max_concurrency=args.max_concurrency,
            case_ids=args.filter_case_id or None,
        )
    elif args.mode == "dataset":
        summary = run_dataset_eval(
            dataset_name=args.dataset_name,
            experiment_name=args.experiment_name,
            max_concurrency=args.max_concurrency,
            case_ids=args.filter_case_id or None,
        )
    else:
        summary = run_single_case_eval(
            dataset_name=args.dataset_name,
            case_id=args.case_id,
            question=args.question,
            must_include=args.must_include,
            experiment_name=args.experiment_name,
            max_concurrency=args.max_concurrency,
        )

    print(f"run_name={summary.run_name}")
    print(f"dataset_run_id={summary.dataset_run_id}")
    print(f"dataset_run_url={summary.dataset_run_url}")
    if args.summary_only:
        print(summary.compact_summary_text)
    else:
        print(summary.result_text)
        print("\n=== Compact Summary ===")
        print(summary.compact_summary_text)


if __name__ == "__main__":
    main()

    # ===== 标准用法（可直接复制）=====
    # 1) 人类可读逐条评测（串行，逐条输出：问题/答案/标准答案/耗时/评分）
    # python scripts/langfuse_eval_minimal.py \
    #   --mode human-json \
    #   --dataset-json-path scripts/data/langfuse_teacher_dataset.json
    #
    # 2) 人类可读逐条评测 + 每条暂停（回车继续，输入 q 退出）
    # python scripts/langfuse_eval_minimal.py \
    #   --mode human-json \
    #   --dataset-json-path scripts/data/langfuse_teacher_dataset.json \
    #   --pause-each-item
    #
    # 3) 先把 JSON 样本导入 Langfuse dataset，再批量评测（支持并发）
    # python scripts/langfuse_eval_minimal.py \
    #   --mode dataset-json \
    #   --dataset-name tt-ai-nl2sql-teacher-44 \
    #   --dataset-json-path scripts/data/langfuse_teacher_dataset.json \
    #   --experiment-name nl2sql-dataset-batch-eval \
    #   --max-concurrency 4
    #
    # 4) 直接评测已有 Langfuse dataset（支持并发）
    # python scripts/langfuse_eval_minimal.py \
    #   --mode dataset \
    #   --dataset-name tt-ai-nl2sql-teacher-44 \
    #   --experiment-name nl2sql-dataset-batch-eval \
    #   --max-concurrency 4
    #
    # 5) 只跑指定 case（可重复传 --filter-case-id）
    # python scripts/langfuse_eval_minimal.py \
    #   --mode human-json \
    #   --dataset-json-path scripts/data/langfuse_teacher_dataset.json \
    #   --filter-case-id teacher-001 \
    #   --filter-case-id teacher-005
    #
    # 6) 单题调试（快速验证）
    # python scripts/langfuse_eval_minimal.py \
    #   --mode single \
    #   --dataset-name tt-ai-nl2sql-minimal \
    #   --case-id case-debug-001 \
    #   --question "查询安装工单的总数" \
    #   --must-include "工单"
