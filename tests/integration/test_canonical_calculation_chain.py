"""H19i integrated evidence for the canonical approved-calculation chain.

Chain under test -- every link is the REAL production component, no mocks:

    PlanCompiler(calculation_catalog=...)
      -> PlanValidator(calculation_catalog=...).validate_query_plan
      -> PlanCompiler.compile  (canonical DAG)
      -> PlanValidator.validate_execution_plan
      -> PlanExecutor -> GatewayMetricStepRunner(real MetricQueryCompiler,
         real QueryGateway) -> real PostgreSQL fixture datasource
      -> RegistryTrustedCalculationRunner (real registered "ratio" template)
      -> ground_execution_answer (the engine real grounding seam)

The database-backed test is opt-in exactly like the other Docker integration
contracts: set TTAI_RUN_POSTGRES_INTEGRATION=1.  The datasource is a throwaway
synthetic PostgreSQL container seeded by this module; no enterprise database is
touched.

WHAT THE EXECUTION-LEVEL EVIDENCE PROVES
  * a real catalog binding makes the compiler emit the canonical DAG
    (two dependency fetches + trusted calculation + verify);
  * the real validator allows exactly that plan;
  * both dependency fetches are compiled by the real MetricQueryCompiler and
    executed by the real QueryGateway against a real database, and the real
    template computes the ratio from those scalar values;
  * the real grounding seam produces exactly ONE grounded fact, owned by the
    calculation receipt, for the requested canonical metric.

WHAT IT DOES NOT PROVE
  * no production authority source for ApprovedCalculationBinding exists yet
    (H19g): this fixture catalog is injected explicitly, exactly as a future
    production loader would have to supply one;
  * the default production wiring builds PlanCompiler() and
    metric_plan_executor(compiler, gateway) WITHOUT a catalog, so this chain is
    NOT reachable through it -- proved by
    test_default_wiring_cannot_reach_the_canonical_chain;
  * it proves no accuracy, no enterprise source and no approved business metric
    definition.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.nl2sql.agents.dynamic_calc.trusted_templates import trusted_template_registry
from src.nl2sql.contracts import (
    ContextBundle,
    FetchMetricStep,
    QueryPlan,
    RequestIdentity,
    TimeRange,
    TrustedCalculationStep,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    ApprovedCalculationInput,
)
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.execution import PlanStepError
from src.nl2sql.orchestration.grounding import ground_execution_answer
from src.nl2sql.orchestration.metric_query import (
    EligibilityPolicy,
    GatewayMetricStepRunner,
    MetricQueryCompiler,
    RelationBinding,
    aggregate_definition_checksum,
    metric_plan_executor,
)
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.semantic.authoring import validate_authoring_ir
from src.nl2sql.semantic.materialization import materialize_authoring_ir
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    MetricContract,
    load_metric_catalog,
    metric_catalog_ir,
)
from src.nl2sql.semantic.registry import SemanticRelease, SemanticReleaseState
from src.nl2sql.semantic.schema_snapshot import (
    ColumnSnapshot,
    IndexSnapshot,
    OrganizationCoverageBinding,
    RelationSnapshot,
    SchemaSnapshot,
    SchemaSnapshotCandidate,
    SchemaSnapshotState,
)

RELEASE_ID = "11111111-1111-1111-1111-111111111111"
SNAPSHOT_ID = "22222222-2222-2222-2222-222222222222"
RELATION = "ai_views.complaint_orders"
CANONICAL_METRIC = "metric.chain_first_response_ratio"
NUMERATOR_METRIC = "metric.chain_numerator_count"
DENOMINATOR_METRIC = "metric.chain_denominator_count"
NOW = datetime(2026, 9, 1, tzinfo=UTC)

RUN_INTEGRATION = os.environ.get("TTAI_RUN_POSTGRES_INTEGRATION") == "1"

# The synthetic relation is declared with exactly the columns the fixture SQL
# creates; the snapshot is deployment input, never derived from the database.
_COLUMN_TYPES: dict[str, str] = {
    "acceptance_time": "timestamp with time zone",
    "completion_time": "timestamp with time zone",
    "is_valid_for_metrics": "boolean",
    "has_valid_bandwidth": "boolean",
    "is_first_response_on_time": "boolean",
}

# Seeded so the canonical ratio is exactly 3/5: five eligible rows inside the
# Asia/Shanghai 2024-02-29 window carry a valid bandwidth, of which three are
# still in transit (completion_time IS NULL).  One row fails the bandwidth
# predicate and one falls outside the requested window.
_SEED_SQL = """
CREATE SCHEMA ai_views;
CREATE TABLE ai_views.complaint_orders (
    acceptance_time timestamptz,
    completion_time timestamptz,
    is_valid_for_metrics boolean,
    has_valid_bandwidth boolean,
    is_first_response_on_time boolean
);
CREATE INDEX complaint_time ON ai_views.complaint_orders (acceptance_time);
INSERT INTO ai_views.complaint_orders VALUES
    ('2024-02-29 10:00:00+08', NULL, TRUE, TRUE, TRUE),
    ('2024-02-29 11:00:00+08', NULL, TRUE, TRUE, FALSE),
    ('2024-02-29 12:00:00+08', '2024-02-29 13:00:00+08', TRUE, TRUE, TRUE),
    ('2024-02-29 13:00:00+08', '2024-02-29 14:00:00+08', TRUE, TRUE, TRUE),
    ('2024-02-29 14:00:00+08', NULL, TRUE, TRUE, FALSE),
    ('2024-02-29 15:00:00+08', NULL, TRUE, FALSE, TRUE),
    ('2024-02-28 10:00:00+08', NULL, TRUE, TRUE, TRUE);
ANALYZE ai_views.complaint_orders;
CREATE ROLE {reader} LOGIN PASSWORD '{password}';
GRANT CONNECT ON DATABASE {database} TO {reader};
GRANT USAGE ON SCHEMA ai_views TO {reader};
GRANT SELECT ON ALL TABLES IN SCHEMA ai_views TO {reader};
"""


def _docker(
    *arguments: str, check: bool = True, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        rendered = " ".join(arguments[:2])
        raise AssertionError(
            f"docker command failed ({rendered}):\n{result.stdout}\n{result.stderr}"
        )
    return result


def _container_logs(container: str) -> str:
    return _docker("logs", container, check=False).stdout[-4000:]


def _wait_for_postgres(container: str, owner: str, database: str) -> None:
    for _ in range(90):
        probe = _docker(
            "exec",
            container,
            "pg_isready",
            "--username",
            owner,
            "--dbname",
            database,
            check=False,
            timeout=15,
        )
        if probe.returncode == 0:
            return
        time.sleep(1)
    pytest.fail(f"PostgreSQL fixture never became ready:\n{_container_logs(container)}")


def _published_port(container: str) -> int:
    result = _docker("port", container, "5432/tcp")
    return int(result.stdout.strip().rsplit(":", maxsplit=1)[1])


def _async_url(role: str, password: str, port: int, database: str) -> str:
    return (
        f"postgresql+asyncpg://{quote(role, safe='')}:{quote(password, safe='')}"
        f"@127.0.0.1:{port}/{quote(database, safe='')}"
    )


@dataclass(frozen=True)
class _ChainPostgres:
    reader_url: str = field(repr=False)


@pytest.fixture(scope="module")
def chain_postgres() -> Iterator[_ChainPostgres]:
    """One throwaway PostgreSQL fixture datasource for the whole module."""

    if shutil.which("docker") is None:
        pytest.fail("Docker is required when TTAI_RUN_POSTGRES_INTEGRATION=1")
    info = _docker("info", check=False, timeout=30)
    if info.returncode != 0:
        pytest.fail(f"Docker daemon is unavailable: {info.stderr.strip()}")

    suffix = uuid.uuid4().hex[:12]
    container = f"ttai-h19i-chain-{suffix}"
    database = "ttai_chain"
    owner = "chain_owner"
    reader = "business_reader"
    owner_password = secrets.token_hex(24)
    reader_password = secrets.token_hex(24)

    try:
        _docker(
            "run",
            "--detach",
            "--name",
            container,
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_DB={database}",
            "--env",
            f"POSTGRES_USER={owner}",
            "--env",
            f"POSTGRES_PASSWORD={owner_password}",
            "postgres:17-alpine",
            timeout=120,
        )
        _wait_for_postgres(container, owner, database)
        _docker(
            "exec",
            container,
            "psql",
            "--no-psqlrc",
            "--username",
            owner,
            "--dbname",
            database,
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            _SEED_SQL.format(reader=reader, password=reader_password, database=database),
        )
        yield _ChainPostgres(
            reader_url=_async_url(
                reader, reader_password, _published_port(container), database
            )
        )
    finally:
        _docker("rm", "--force", "--volumes", container, check=False, timeout=30)


def _gateway(url: str) -> tuple[AsyncEngine, QueryGateway]:
    engine = create_async_engine(url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, QueryGateway(
        sessions, schema="ai_views", readonly_role="business_reader"
    )


def _count_contract(
    metric_key: str,
    *,
    display_name: str,
    predicates: tuple[dict[str, str], ...] | None = None,
) -> MetricContract:
    seed_path = Path(__file__).resolve().parents[2] / "config/metrics/complaint.yaml"
    seed = load_metric_catalog(seed_path.read_text(encoding="utf-8")).metrics[0]
    payload = seed.model_dump(mode="json")
    payload.update(
        metric_key=metric_key,
        display_name=display_name,
        formula_version=f"{metric_key}.v1",
        owner="synthetic-owner",
        approver="synthetic-approver",
        release_status="active",
        freshness_sla_seconds=86400,
    )
    if predicates is not None:
        payload["predicates"] = list(predicates)
    return MetricContract.model_validate(payload)


class _ChainAuthority:
    """The real release/snapshot/binding/catalog used by the chain fixture."""

    def __init__(self) -> None:
        self.numerator = _count_contract(
            "chain_numerator_count", display_name="chain numerator"
        )
        self.denominator = _count_contract(
            "chain_denominator_count",
            display_name="chain denominator",
            predicates=({"field": "has_valid_bandwidth", "operator": "is_true"},),
        )
        relation = RelationSnapshot(
            relation_id=RELATION,
            schema_name="ai_views",
            relation_name="complaint_orders",
            relation_kind="table",
            columns=tuple(
                ColumnSnapshot(name, data_type, True, index)
                for index, (name, data_type) in enumerate(_COLUMN_TYPES.items(), 1)
            ),
            primary_key=(),
            foreign_keys=(),
            indexes=(IndexSnapshot("complaint_time", ("acceptance_time",), False, False),),
            partition_key=None,
            parent_relation_id=None,
            estimated_rows=12,
            total_bytes=8192,
            sensitivity="internal",
            sensitive_columns=(),
            aggregate_coverage=(),
            freshness_sla_seconds=None,
            organization_coverage=(OrganizationCoverageBinding("city_company"),),
        )
        candidate = SchemaSnapshotCandidate(
            "generated-fixture", ("ai_views",), (relation,), "a" * 64, "b" * 64
        )
        self.snapshot = SchemaSnapshot(
            SNAPSHOT_ID,
            SchemaSnapshotState.VALIDATED,
            candidate,
            {"ok": True},
            NOW,
            NOW,
        )
        ir = metric_catalog_ir(
            MetricCatalog(metrics=(self.numerator, self.denominator)),
            relations={self.numerator.source_ref: RELATION},
        )
        report = validate_authoring_ir(ir, relation_columns={RELATION: _COLUMN_TYPES})
        assert report.ok, report.to_dict()
        release_candidate = materialize_authoring_ir(ir, report)
        relation_asset = next(
            asset for asset in release_candidate.assets if asset.asset_type == "relation"
        )
        self.release = SemanticRelease(
            release_id=RELEASE_ID,
            version=1,
            checksum=release_candidate.checksum,
            state=SemanticReleaseState.ACTIVE,
            documents=release_candidate.documents,
            validation_report=report.to_dict(),
            change_summary="synthetic H19i chain fixture",
            previous_release_id=None,
            created_at=NOW,
            schema_snapshot_id=SNAPSHOT_ID,
            schema_snapshot_checksum=candidate.checksum,
        )
        self.relation_binding = RelationBinding(
            source_ref=self.numerator.source_ref,
            relation_asset_id=relation_asset.asset_id,
            schema_name="ai_views",
            relation_name="complaint_orders",
            allowed_columns=tuple(_COLUMN_TYPES),
            required_permissions=("metrics:read",),
            approved=True,
            timestamp_kind="timestamptz",
        )
        self.identity = RequestIdentity(
            request_id=UUID(RELEASE_ID),
            user_id="synthetic-reader",
            permissions=frozenset({"metrics:read", "nl2sql:invoke"}),
        )
        # The canonical output identity is RESOLVED in the request context by the
        # deployment; the fixture states it explicitly because no production
        # release -> binding catalog exists yet (H19g).
        self.context = ContextBundle(
            semantic_release_id=UUID(RELEASE_ID),
            schema_snapshot_id=UUID(SNAPSHOT_ID),
            domains=("complaint",),
            asset_ids=(
                self.numerator.asset_id,
                self.denominator.asset_id,
                CANONICAL_METRIC,
            ),
            approved_relation_ids=(relation_asset.asset_id,),
            resolution_status="resolved",
        )
        metadata = trusted_template_registry.metadata("ratio")
        self.binding = ApprovedCalculationBinding(
            canonical_metric_key=CANONICAL_METRIC,
            template_id="ratio",
            template_version=metadata.version,
            template_checksum=metadata.checksum,
            inputs=(
                ApprovedCalculationInput(
                    role="numerator",
                    metric_key=self.numerator.asset_id,
                    metric_contract_sha256=aggregate_definition_checksum(self.numerator),
                ),
                ApprovedCalculationInput(
                    role="denominator",
                    metric_key=self.denominator.asset_id,
                    metric_contract_sha256=aggregate_definition_checksum(self.denominator),
                ),
            ),
            binding_revision="h19i-chain-rev-1",
            semantic_release_id=RELEASE_ID,
            semantic_release_checksum=self.release.checksum,
        )
        self.catalog = ApprovedCalculationCatalog([self.binding])

    async def read_active(self) -> SemanticRelease:
        return self.release

    async def read_snapshot(self, snapshot_id: str) -> SchemaSnapshot:
        assert snapshot_id == SNAPSHOT_ID
        return self.snapshot

    @property
    def compiler(self) -> MetricQueryCompiler:
        return MetricQueryCompiler(
            read_active=self.read_active,
            read_snapshot=self.read_snapshot,
            bindings=(self.relation_binding,),
            eligibility_policies=(
                EligibilityPolicy(policy_id=self.numerator.eligibility_policy_id),
            ),
            identity=self.identity,
        )

    def plan(self) -> QueryPlan:
        return QueryPlan(
            intent="metric",
            domain="complaint",
            metric_keys=(CANONICAL_METRIC,),
            time_range=TimeRange(start=date(2024, 2, 29), end=date(2024, 2, 29)),
            grain="day",
            source_strategy="aggregate_first",
        )


@pytest.mark.postgres_integration
@pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="set TTAI_RUN_POSTGRES_INTEGRATION=1 to run Docker PostgreSQL contracts",
)
@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_canonical_chain_executes_against_a_real_datasource(
    chain_postgres: _ChainPostgres,
) -> None:
    """Drive compiler -> validator -> runner -> calculator -> grounding for real."""

    authority = _ChainAuthority()
    plan = authority.plan()
    validator = PlanValidator(calculation_catalog=authority.catalog)

    query_validation = validator.validate_query_plan(
        plan=plan, context=authority.context, identity=authority.identity
    )
    print(f"[chain] 1 validate_query_plan outcome={query_validation.outcome}")
    assert query_validation.outcome == "allow"
    assert query_validation.issues == ()

    execution_plan = PlanCompiler(calculation_catalog=authority.catalog).compile(
        plan=plan, context=authority.context, validation=query_validation
    )
    print(
        "[chain] 2 compiled steps="
        f"{[(step.step_id, step.kind) for step in execution_plan.steps]}"
    )
    assert [step.kind for step in execution_plan.steps] == [
        "fetch_metric",
        "fetch_metric",
        "trusted_calculation",
        "verify",
    ]
    assert all(
        CANONICAL_METRIC not in step.metric_keys
        for step in execution_plan.steps
        if isinstance(step, FetchMetricStep)
    )
    assert any(
        isinstance(step, TrustedCalculationStep)
        and step.output_metric_key == CANONICAL_METRIC
        for step in execution_plan.steps
    )

    budget = RouteBudgetLedger(route="standard")
    execution_validation = validator.validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=authority.context,
        route_budget=budget.limits,
    )
    print(f"[chain] 3 validate_execution_plan outcome={execution_validation.outcome}")
    assert execution_validation.outcome == "allow", execution_validation.issues

    engine, gateway = _gateway(chain_postgres.reader_url)
    try:
        executor = metric_plan_executor(
            authority.compiler, gateway, calculation_catalog=authority.catalog
        )
        result = await executor.execute(
            query_plan=plan,
            context=authority.context,
            execution_plan=execution_plan,
            validation=execution_validation,
            expected_policy_version=validator.policy_version,
            expected_policy_checksum=validator.policy_checksum,
            budget=budget,
            deadline_ms=10_000,
        )
    finally:
        await engine.dispose()

    print(
        f"[chain] 4 execution status={result.record.status} "
        f"stop_reason={result.record.stop_reason} sql_executions={budget.sql_executions}"
    )
    for receipt in result.record.step_receipts:
        digest = (receipt.rowset_sha256 or "-")[:12]
        print(
            f"[chain]   receipt {receipt.step_id} kind={receipt.kind} "
            f"status={receipt.status} rowset_sha256={digest} "
            f"error={receipt.error_code}"
        )

    assert result.record.status == "succeeded"
    assert result.record.stop_reason is None
    assert budget.sql_executions == 2
    assert result.outputs["fetch_numerator"] == {"value": 3}
    assert result.outputs["fetch_denominator"] == {"value": 5}
    assert result.outputs["calculate_metric"] == {"ratio": 0.6}
    assert result.outputs["verify_result"]["verified"] is True

    receipts = {receipt.step_id: receipt for receipt in result.record.step_receipts}
    assert receipts["fetch_numerator"].rowset_sha256 is not None
    assert receipts["fetch_denominator"].rowset_sha256 is not None
    calculation_receipt = receipts["calculate_metric"]
    assert calculation_receipt.kind == "trusted_calculation"
    assert calculation_receipt.binding_checksum == authority.binding.checksum
    assert calculation_receipt.output_metric_key == CANONICAL_METRIC
    assert calculation_receipt.template_id == authority.binding.template_id
    assert calculation_receipt.template_version == authority.binding.template_version

    grounded = ground_execution_answer(
        query_plan=plan,
        execution_plan=execution_plan,
        record=result.record,
        outputs=result.outputs,
    )
    print(
        "[chain] 5 grounded facts="
        f"{[(fact.metric_key, fact.status, fact.value) for fact in grounded.facts]}"
    )
    print(f"[chain]   answer_text={grounded.answer_text!r}")
    print(f"[chain]   degradation_flags={grounded.artifact.degradation_flags}")

    assert [fact.metric_key for fact in grounded.facts] == [CANONICAL_METRIC]
    fact = grounded.facts[0]
    assert fact.step_id == "calculate_metric"
    assert fact.status == "grounded"
    assert fact.value == 0.6
    assert fact.output_digest == calculation_receipt.output_digest
    assert "grounding_execution_mismatch" not in grounded.artifact.degradation_flags
    assert NUMERATOR_METRIC not in grounded.answer_text
    assert DENOMINATOR_METRIC not in grounded.answer_text


@pytest.mark.asyncio
async def test_default_wiring_cannot_reach_the_canonical_chain() -> None:
    """The production-shaped default wiring (no injected catalog) cannot reach it.

    engine.create_v2_engine resolves PlanCompiler() and
    typed_runtime.build_request_typed_runtime builds
    metric_plan_executor(compiler, gateway); both leave the calculation catalog
    at its None default.  This test reproduces exactly those two call shapes with
    real components and observes the reachability boundary at execution level
    (no database is needed: the refusal happens before SQL).
    """

    authority = _ChainAuthority()
    plan = authority.plan()

    validator = PlanValidator()
    query_validation = validator.validate_query_plan(
        plan=plan, context=authority.context, identity=authority.identity
    )
    assert query_validation.outcome == "allow"

    execution_plan = PlanCompiler().compile(
        plan=plan, context=authority.context, validation=query_validation
    )
    print(
        "[chain] default wiring steps="
        f"{[(step.step_id, step.kind) for step in execution_plan.steps]}"
    )
    assert [step.kind for step in execution_plan.steps] == ["fetch_metric", "verify"]
    assert not any(
        isinstance(step, TrustedCalculationStep) for step in execution_plan.steps
    )

    budget = RouteBudgetLedger(route="standard")
    execution_validation = validator.validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=authority.context,
        route_budget=budget.limits,
    )
    assert execution_validation.outcome == "allow"

    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    executor = metric_plan_executor(authority.compiler, gateway)
    result = await executor.execute(
        query_plan=plan,
        context=authority.context,
        execution_plan=execution_plan,
        validation=execution_validation,
        expected_policy_version=validator.policy_version,
        expected_policy_checksum=validator.policy_checksum,
        budget=budget,
        deadline_ms=10_000,
    )
    print(
        f"[chain] default wiring status={result.record.status} "
        f"stop_reason={result.record.stop_reason}"
    )
    assert result.record.status == "failed"
    assert result.record.stop_reason == "metric_contract_missing"

    dependency_fetch = FetchMetricStep(
        step_id="fetch_numerator",
        metric_keys=(NUMERATOR_METRIC,),
        calculation_input_role="numerator",
        calculation_binding_checksum=authority.binding.checksum,
        calculation_output_metric_key=CANONICAL_METRIC,
    )
    runner = GatewayMetricStepRunner(authority.compiler, gateway)
    with pytest.raises(PlanStepError) as refused:
        await runner.prepare(
            step=dependency_fetch, query_plan=plan, context=authority.context
        )
    print(f"[chain] default wiring dependency fetch refused: {refused.value.code}")
    assert refused.value.code == "metric_dependency_binding_missing"
