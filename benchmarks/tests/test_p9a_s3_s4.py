"""P9A S3 (executor adapter + typed default) and S4 (selection) evidence.

Every assertion here is execution-level.  No source-string assertion is used to
claim that a path is reachable: the length of this file exists because "the
wiring exists" is not the same statement as "the path is reachable", and this
project has already paid for that distinction.

Nothing in this file claims a real accuracy number.  The demo/fixture runs are
harness evidence and are labelled evidence_kind="harness".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import benchmarks.agent_bridge as agent_bridge
import benchmarks.runner as runner
from benchmarks.adapters import BenchmarkCase, benchmark_case_to_eval_case
from benchmarks.executor_adapter import (
    EVIDENCE_HARNESS,
    TEXT_STATE_KEYS,
    DemoEngineInvoker,
    ExecutorAdapter,
    TypedRunProductError,
    build_demo_engine_executor,
    build_receipt,
    build_unavailable_typed_executor,
    product_from_engine_state,
)
from benchmarks.registry import (
    ORACLE_VERSION,
    CaseOracle,
    EvalCase,
    case_set_checksum,
)
from benchmarks.runner import (
    build_manifest,
    build_typed_executor,
    load_cases,
    run_typed_benchmark,
    run_typed_dataset,
)
from benchmarks.selection import (
    CONSERVATIVE_REASON,
    MAPPED_REASON,
    conservative_covers_safety_subset,
    select_cases,
    verify_selection_checksum,
)
from benchmarks.typed_receipts import (
    BenchmarkManifest,
    BudgetGate,
    TypedAnswerReceipt,
)

_SHA = "a" * 64
_DEMO_DATASET = "p9a_typed"


def _manifest(**overrides: object) -> BenchmarkManifest:
    payload: dict[str, object] = {
        "run_id": "s3-run",
        "dataset_checksum": _SHA,
        "prompt_version": "p-v1",
        "policy_version": "p-v1",
        "semantic_version": "p-v1",
        "model_profile_version": "p-v1",
        "git_revision": "abcdef0",
    }
    payload.update(overrides)
    return BenchmarkManifest.model_validate(payload)


def _demo_case(question: str = "revenue") -> BenchmarkCase:
    return BenchmarkCase(
        case_id=f"probe-{question}",
        source="unit",
        layer="L1",
        domain="demo",
        question=question,
    )


# ── S3: structured product -> receipt, with NO text parsing ─────────────────


def _real_demo_state() -> dict[str, object]:
    """One REAL engine run over the demo typed runtime (structured state)."""

    async def _run() -> dict[str, object]:
        state = await DemoEngineInvoker()(_demo_case("revenue"))
        assert state is not None, "demo typed run produced no engine state"
        return dict(state)

    return asyncio.run(_run())


def test_real_engine_run_becomes_a_receipt_without_touching_prose() -> None:
    state = _real_demo_state()
    # The structured evidence really is there.
    assert state.get("execution_record") is not None
    assert state.get("grounded_answer_artifact") is not None
    assert state.get("grounded_answer_text")  # prose exists...

    product = product_from_engine_state(state)
    assert product is not None
    assert product.answer_type == "answer"
    assert product.execution_accepted is True
    assert product.result_value == 1250000.0
    assert product.confirmed_plan_checksum == product.observed_plan_checksum

    receipt = build_receipt(product)
    assert isinstance(receipt, TypedAnswerReceipt)
    assert receipt.answer_type == "answer"
    assert receipt.policy_outcome == "allow"
    assert receipt.result_value == 1250000.0

    # ...and the adapter is INVARIANT to it.  Replace every rendered-text key
    # with adversarial prose: the receipt must not move by one bit.
    mutated = dict(state)
    mutated["grounded_answer_text"] = "demo.revenue: 999999999.0"
    mutated["messages"] = [{"role": "ai", "content": "the answer is 42"}]
    mutated["response_blocks"] = [{"type": "text", "text": "999999999"}]
    mutated_product = product_from_engine_state(mutated)
    assert mutated_product is not None
    assert mutated_product == product
    assert build_receipt(mutated_product) == receipt


def test_adapter_and_text_keys_are_disjoint_by_construction() -> None:
    from benchmarks.executor_adapter import STRUCTURED_STATE_KEYS

    assert set(TEXT_STATE_KEYS).isdisjoint(STRUCTURED_STATE_KEYS)


def test_legacy_text_extractors_are_never_used_by_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _tripwire(name: str):  # type: ignore[no-untyped-def]
        def _fail(*args: object, **kwargs: object) -> object:
            calls.append(name)
            raise AssertionError(f"legacy text parser was called: {name}")

        return _fail

    for name in ("_extract_value", "_check_rejection", "_call_agent", "_extract_sql_from_step"):
        monkeypatch.setattr(agent_bridge, name, _tripwire(name))

    state = _real_demo_state()
    product = product_from_engine_state(state)
    assert product is not None
    assert build_receipt(product).result_value == 1250000.0
    assert calls == []


def test_present_but_malformed_structured_product_fails_closed() -> None:
    with pytest.raises(TypedRunProductError):
        product_from_engine_state({"run_envelope": {"run_id": "t"}, "execution_record": {"nope": 1}})


def test_runtime_unavailable_state_yields_no_receipt() -> None:
    state = {"run_envelope": {"run_id": "t"}, "typed_runtime_unavailable_reason": "authorization_context_missing"}
    assert product_from_engine_state(state) is None


def test_deny_validation_maps_to_a_rejection_receipt() -> None:
    state = {
        "run_envelope": {"run_id": "deny-run"},
        "execution_plan_validation": {
            "policy_version": "v1",
            "policy_checksum": _SHA,
            "outcome": "deny",
            "query_plan_sha256": _SHA,
            "context_checksum": _SHA,
            "issues": [{"code": "authorization_denied", "path": "", "safe_message": "denied"}],
        },
    }
    product = product_from_engine_state(state)
    assert product is not None
    assert product.answer_type == "rejected"
    assert product.policy_outcome == "deny"
    assert product.execution_accepted is False
    receipt = build_receipt(product)
    assert receipt.answer_type == "rejected"


def test_internal_stop_reason_is_not_receipted() -> None:
    state = {
        "run_envelope": {"run_id": "stop-run"},
        "stop_reason": "query_plan_proposal_failed",
    }
    assert product_from_engine_state(state) is None


@pytest.mark.parametrize(
    ("decision_kind", "expected_type", "expected_policy"),
    [
        ("business_confirmation", "hitl", "approval"),
        ("risk_policy_decision", "hitl", "approval"),
        ("clarification", "clarification", "allow"),
    ],
)
def test_pending_decision_kind_maps_to_the_right_terminal(
    decision_kind: str, expected_type: str, expected_policy: str
) -> None:
    state = {
        "run_envelope": {"run_id": f"decision-{decision_kind}"},
        "pending_decision": {
            "decision_kind": decision_kind,
            "unresolved_slots": ["metric"] if decision_kind == "clarification" else [],
        },
    }
    product = product_from_engine_state(state)
    assert product is not None
    assert product.answer_type == expected_type
    assert product.policy_outcome == expected_policy
    assert product.execution_accepted is False


@pytest.mark.asyncio
async def test_missing_receipt_is_provenance_failure_not_a_silent_pass() -> None:
    case = benchmark_case_to_eval_case(
        BenchmarkCase(
            case_id="needs-receipt",
            source="unit",
            layer="L1",
            domain="d",
            question="q",
            gold_value=1,
        )
    )
    executor = build_unavailable_typed_executor()
    report = await run_typed_benchmark(
        [case],
        manifest=_manifest(),
        executor=executor,
        budget=BudgetGate(max_total_cost=1.0, max_calls=5),
    )
    result = report.results[0]
    assert result.receipt_required is True
    assert result.receipt_present is False
    assert result.observed_outcome == "PROVENANCE_FAILURE"
    assert result.passed is False
    assert report.receipts_present == 0
    assert report.accuracy_established is False
    assert report.evidence_kind == "typed_runtime_unavailable"


@pytest.mark.asyncio
async def test_real_typed_run_over_the_engine_produces_an_adjudicated_answer() -> None:
    """The reachable S3 path: real engine -> structured state -> receipt -> verdict."""
    cases = [
        benchmark_case_to_eval_case(case)
        for case in load_cases(_DEMO_DATASET)
        if case.case_id in {"p9a-typed-query-revenue", "p9a-typed-query-cost"}
    ]
    assert len(cases) == 2
    report = await run_typed_benchmark(
        cases,
        manifest=_manifest(),
        executor=build_demo_engine_executor(),
        budget=BudgetGate(max_total_cost=1.0, max_calls=10),
    )
    assert report.evidence_kind == EVIDENCE_HARNESS
    assert report.receipts_present == 2
    assert report.accuracy_established is True
    outcomes = {result.case_id: result.observed_outcome for result in report.results}
    assert outcomes == {
        "p9a-typed-query-revenue": "CORRECT_ANSWER",
        "p9a-typed-query-cost": "CORRECT_ANSWER",
    }
    values = sorted(result.output_value for result in report.results)
    assert values == [780000.0, 1250000.0]


@pytest.mark.asyncio
async def test_typed_runner_marks_receipt_required_and_legacy_bridge_does_not() -> None:
    typed_case = benchmark_case_to_eval_case(
        BenchmarkCase(case_id="t", source="unit", layer="L1", domain="d", question="q", gold_value=1)
    )
    typed_report = await run_typed_benchmark(
        [typed_case],
        manifest=_manifest(),
        executor=build_unavailable_typed_executor(),
        budget=BudgetGate(max_total_cost=1.0, max_calls=5),
    )
    assert typed_report.results[0].receipt_required is True

    legacy = await agent_bridge.execute_case(_demo_case("revenue"))
    assert legacy.receipt_required is False


@pytest.mark.asyncio
async def test_agent_bridge_can_be_obliged_to_a_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_call(question: str, session_id: str) -> tuple[str, str]:
        return "revenue is 1250000", "SELECT 1250000"

    monkeypatch.setattr(agent_bridge, "_call_agent", _fake_call)
    case = BenchmarkCase(
        case_id="receipt-obliged",
        source="unit",
        layer="L1",
        domain="demo",
        question="revenue",
        gold_value=1250000.0,
    )
    result = await agent_bridge.execute_case(case, receipt_required=True)
    assert result.receipt_required is True
    assert result.receipt_present is False
    assert result.observed_outcome == "PROVENANCE_FAILURE"
    assert result.passed is False


# ── S3: CLI default path reaches the typed runner and no text parser ────────


def test_cli_default_is_typed_and_never_calls_a_legacy_text_parser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def _tripwire(name: str):  # type: ignore[no-untyped-def]
        def _fail(*args: object, **kwargs: object) -> object:
            calls.append(name)
            raise AssertionError(f"legacy path reached: {name}")

        return _fail

    for name in ("_extract_value", "_check_rejection", "_call_agent", "_extract_sql_from_step"):
        monkeypatch.setattr(agent_bridge, name, _tripwire(name))
    monkeypatch.setattr(runner, "execute_case", _tripwire("execute_case"))

    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmarks.runner",
            "--dataset",
            _DEMO_DATASET,
            "--typed-runtime",
            "production",
            "--output-dir",
            str(tmp_path),
        ],
    )
    runner.main()
    out = capsys.readouterr().out

    assert "[路径] typed" in out
    assert "Evidence kind: typed_runtime_unavailable" in out
    assert "receipts_present: 0/5" in out
    assert "accuracy_established: False" in out
    assert 'mode slices: ["ANALYZE", "BUILD", "QUERY"]' in out
    assert calls == []
    # The manifest written to disk carries the same selection checksum.
    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["evidence_kind"] == "typed_runtime_unavailable"
    assert payload["manifest"]["case_selection_checksum"]
    assert payload["manifest"]["selection_version"] == "p9a-selection-1.0"


def test_cli_legacy_bridge_is_explicit_and_labelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[bool] = []

    async def _fake_execute(case: BenchmarkCase, **kwargs: object) -> object:
        from benchmarks.metrics import CaseResult, adjudicate_case_result

        seen.append(True)
        return adjudicate_case_result(
            CaseResult(
                case_id=case.case_id,
                layer=case.layer,
                domain=case.domain,
                expected_mode=case.expected_mode,
                execution_success=bool(case.question),
            )
        )

    monkeypatch.setattr(runner, "execute_case", _fake_execute)
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmarks.runner",
            "--dataset",
            _DEMO_DATASET,
            "--legacy-bridge",
            "--output-dir",
            str(tmp_path),
        ],
    )
    runner.main()
    out = capsys.readouterr().out
    assert "[路径] legacy text bridge (receipt_required=False)" in out
    assert "Evidence kind: legacy-text" in out
    assert seen == [True] * 5


# ── S4: selection ──────────────────────────────────────────────────────────


def _eval_case(
    case_id: str,
    *,
    mode: str = "QUERY",
    capability: str = "fetch",
    risk: str = "low",
    tags: tuple[str, ...] = (),
    outcome: str = "CORRECT_ANSWER",
) -> EvalCase:
    if outcome == "CORRECT_ANSWER":
        oracle = CaseOracle(
            oracle_kind="approved_fact", oracle_revision=f"{case_id}-r1", expected_value=1
        )
    else:
        oracle = CaseOracle(oracle_kind="reviewed_label", oracle_revision=f"{case_id}-r1")
    return EvalCase(
        case_id=case_id,
        revision=f"{case_id}-rev",
        source="unit",
        layer="L1",
        domain="d",
        question="q",
        mode=mode,  # type: ignore[arg-type]
        capability=capability,
        risk=risk,  # type: ignore[arg-type]
        tags=tags,
        expected_outcome=outcome,  # type: ignore[arg-type]
        oracle=oracle,
    )


def test_change_maps_to_capability_and_selects_the_affected_cases() -> None:
    cases = [
        _eval_case("f1", capability="fetch"),
        _eval_case("c1", mode="ANALYZE", capability="compute"),
        _eval_case("s1", capability="safety", risk="high", outcome="CORRECT_REJECTION"),
        _eval_case("cl1", capability="clarification", outcome="CORRECT_CLARIFICATION"),
        _eval_case("a1", capability="availability", outcome="CORRECT_RESULT_UNAVAILABLE"),
        _eval_case("b1", mode="BUILD", capability="build"),
    ]
    plan = select_cases(cases, changes=["nl2sql.orchestration.metric_query"])
    assert plan.reason == MAPPED_REASON
    assert plan.resolved_capabilities == ("compute", "fetch")
    keys = set(plan.case_keys())
    # affected + the always-on shared regression set
    assert keys == {"f1::QUERY", "c1::ANALYZE", "s1::QUERY", "cl1::QUERY", "a1::QUERY"}
    assert "b1::BUILD" not in keys
    # the shared safety subset is never dropped
    assert set(plan.shared_regression_keys) <= keys


def test_shared_case_is_deduplicated_but_mode_variants_stay() -> None:
    shared_query = _eval_case("dup", mode="QUERY", capability="build")
    shared_build = _eval_case("dup", mode="BUILD", capability="build")
    duplicate_query = _eval_case("dup", mode="QUERY", capability="build")
    plan = select_cases(
        [shared_query, shared_build, duplicate_query],
        changes=["nl2sql.artifacts.definitions"],
    )
    assert plan.deduplicated == 1
    assert len(plan.cases) == 2
    assert {case.mode for case in plan.cases} == {"QUERY", "BUILD"}
    assert set(plan.case_keys()) == {"dup::QUERY", "dup::BUILD"}


def test_same_seed_same_selection_and_different_seed_can_differ() -> None:
    cases = [_eval_case(f"f{i}") for i in range(10)]
    affected = ["nl2sql.orchestration.metric_query"]
    first = select_cases(cases, changes=affected, seed=0, max_cases=3)
    second = select_cases(cases, changes=affected, seed=0, max_cases=3)
    assert first.checksum == second.checksum
    assert first.case_keys() == second.case_keys()
    assert verify_selection_checksum(first)
    assert len(first.cases) == 3

    vari6 = {
        select_cases(cases, changes=affected, seed=seed, max_cases=3).checksum
        for seed in range(6)
    }
    assert len(vari6) > 1

    repeated = select_cases(
        cases, changes=affected, seed=7, repetitions=2, max_cases=None
    )
    assert len(repeated.runs) == 2
    assert repeated.runs[0].seed != repeated.runs[1].seed
    assert repeated.runs[0].case_keys == repeated.runs[1].case_keys
    again = select_cases(
        cases, changes=affected, seed=7, repetitions=2, max_cases=None
    )
    assert again.checksum == repeated.checksum


def test_unknown_scope_widens_to_the_full_regression_not_a_guess() -> None:
    cases = [
        _eval_case("f1", capability="fetch"),
        _eval_case("c1", mode="ANALYZE", capability="compute"),
        _eval_case("s1", capability="safety", risk="high", outcome="CORRECT_REJECTION"),
        _eval_case("a1", capability="availability", outcome="CORRECT_RESULT_UNAVAILABLE"),
        _eval_case("b1", mode="BUILD", capability="build"),
    ]
    unknown = select_cases(cases, changes=["does.not.exist"], max_cases=2)
    assert unknown.reason == CONSERVATIVE_REASON
    assert len(unknown.cases) == len(cases)  # widened, NOT capped to max_cases
    assert unknown.sampling_skipped is True
    assert conservative_covers_safety_subset(unknown, cases)
    assert set(unknown.case_keys()) > set(unknown.shared_regression_keys)

    empty = select_cases(cases, max_cases=2)
    assert empty.reason == CONSERVATIVE_REASON
    assert len(empty.cases) == len(cases)

    mapped = select_cases(cases, changes=["build"], max_cases=2)
    assert mapped.reason == MAPPED_REASON
    assert conservative_covers_safety_subset(mapped, cases)


def test_selection_checksum_enters_the_manifest_and_is_recomputable() -> None:
    cases = [_eval_case(f"f{i}") for i in range(5)]
    plan = select_cases(cases, changes=["metric_query"], seed=3, repetitions=2)
    manifest = build_manifest(run_id="sel-run", plan=plan, dataset="unit")
    assert manifest.case_selection_checksum == plan.checksum
    assert manifest.selection_version == plan.selection_version
    assert manifest.seed == plan.seed
    assert manifest.registry_revision == "p9a-registry-v1"
    assert manifest.oracle_version == ORACLE_VERSION
    assert manifest.dataset_checksum == case_set_checksum(plan.cases)
    assert plan.recompute_checksum() == plan.checksum
    assert verify_selection_checksum(plan)


def test_typed_dataset_selection_reaches_the_report_and_covers_three_modes() -> None:
    report, plan = run_typed_dataset(
        dataset=_DEMO_DATASET,
        typed_runtime="production",
        run_id="s4-typed-run",
    )
    assert plan.reason == CONSERVATIVE_REASON  # no --change given
    assert set(plan.mode_counts()) == {"QUERY", "ANALYZE", "BUILD"}
    assert set(report.mode_slices) == {"QUERY", "ANALYZE", "BUILD"}
    assert report.manifest["case_selection_checksum"] == plan.checksum
    assert report.manifest["selection_version"] == plan.selection_version
    assert all(result.receipt_required for result in report.results)
    assert report.receipts_present == 0
    assert report.accuracy_established is False


def test_typed_executor_choices_are_explicit() -> None:
    from benchmarks.executor_adapter import UnavailableTypedExecutor

    demo_executor = build_typed_executor("demo")
    assert isinstance(demo_executor, ExecutorAdapter)
    assert demo_executor.evidence_kind == EVIDENCE_HARNESS
    production = build_typed_executor("production")
    assert isinstance(production, UnavailableTypedExecutor)
    assert production.evidence_kind == "typed_runtime_unavailable"
    none_executor = build_typed_executor("none")
    assert isinstance(none_executor, UnavailableTypedExecutor)
    assert none_executor.reason == "typed_runtime_not_requested"


def test_production_default_derives_its_reason_from_the_real_factory_gate() -> None:
    """The default does not assert a reason: it asks the production factory."""
    from benchmarks.executor_adapter import UnavailableTypedExecutor, production_gate_reason

    reason = production_gate_reason()
    assert reason == "authorization_context_missing"
    production = build_typed_executor("production")
    assert isinstance(production, UnavailableTypedExecutor)
    assert production.reason == reason
