from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.adapters import BenchmarkCase
from benchmarks.metrics import CaseResult, generate_report
from benchmarks.runner import run_typed_benchmark, save_report
from benchmarks.typed_receipts import (
    BenchmarkManifest,
    BudgetGate,
    ProviderCallReceipt,
    TypedAnswerReceipt,
    mcnemar_exact,
    paired_bootstrap_interval,
    validate_receipt,
    verify_nvidia_small_model,
)


def _manifest() -> BenchmarkManifest:
    return BenchmarkManifest(
        run_id="fake-run",
        dataset_checksum="a" * 64,
        prompt_version="prompt-v1",
        policy_version="policy-v1",
        semantic_version="semantic-v1",
        model_profile_version="profile-v1",
        git_revision="abcdef0",
        matrix=("deepseek.flash", "deepseek.pro", "benchmark.nim"),
    )


def _receipt(*, alias: str = "fast.default", stage: str = "answer") -> TypedAnswerReceipt:
    return TypedAnswerReceipt(
        trace_id="trace-1",
        answer_type="answer",
        answer_hash="b" * 64,
        rowset_sha256="c" * 64,
        candidate_score=0.9,
        policy_outcome="allow",
        execution_accepted=True,
        execution_row_count=1,
        model_calls=(
            ProviderCallReceipt(
                alias=alias,
                stage=stage,  # type: ignore[arg-type]
                resolved_model="fake-model",
                input_tokens=3,
                output_tokens=2,
                estimated_cost=0.01,
                latency_ms=10,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_fake_provider_typed_receipt_runs_without_parsing_answer_text() -> None:
    cases = [BenchmarkCase(case_id="case-1", source="fake", layer="L1", domain="test", question="sensitive")]

    async def fake_executor(case: BenchmarkCase) -> TypedAnswerReceipt:
        assert case.question == "sensitive"
        return _receipt()

    report = await run_typed_benchmark(cases, manifest=_manifest(), executor=fake_executor, budget=BudgetGate(1, 2))

    assert report.total_cases == 1
    assert report.results[0].execution_success is True
    assert report.results[0].answer_receipt["answer_hash"] == "b" * 64
    assert report.manifest["dataset_checksum"] == "a" * 64


def test_budget_and_pro_stage_gates_and_statistical_outputs() -> None:
    with pytest.raises(ValueError, match="plan stage"):
        validate_receipt(_receipt(alias="plan.pro", stage="answer"))
    gate = BudgetGate(max_total_cost=0.01, max_calls=1)
    gate.consume(_receipt())
    with pytest.raises(RuntimeError, match="budget"):
        gate.consume(_receipt())

    low, high = paired_bootstrap_interval([0, 1, 0], [1, 1, 1], samples=100, seed=7)
    assert low >= 0
    assert high >= low
    assert mcnemar_exact([False, True], [True, True])["better"] == 1


@pytest.mark.asyncio
async def test_nvidia_preflight_requires_configured_model_in_models_response() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"data": [{"id": "approved-small"}]}

    class Client:
        async def get(self, path: str) -> Response:
            assert path == "/models"
            return Response()

    assert await verify_nvidia_small_model(Client(), "approved-small") == "approved-small"


def test_saved_report_redacts_case_id_and_error_details(tmp_path: Path) -> None:
    report = generate_report(
        "redaction-run",
        [
            CaseResult(
                case_id="customer-secret-case",
                layer="L1",
                domain="test",
                expected_mode="sql_only",
                execution_error="provider_error: password=do-not-persist",
            )
        ],
    )

    path = save_report(report, output_dir=tmp_path)
    contents = path.read_text(encoding="utf-8") + path.with_suffix(".md").read_text(encoding="utf-8")

    assert "customer-secret-case" not in contents
    assert "password=do-not-persist" not in contents
