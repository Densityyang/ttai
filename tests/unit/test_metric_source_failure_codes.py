"""Batch-deployment diagnostics for metric source selection.

PlanStepError deliberately carries ONE code, so every code after the first is
only recoverable from the log.  These tests pin that the COMPLETE failure code
list is logged while the exception keeps its single-code contract.
"""

from __future__ import annotations

import logging

import pytest

from src.nl2sql.orchestration.execution import PlanStepError
from tests.metric_fixtures import AggregateAuthority

_LOGGER = "src.nl2sql.orchestration.metric_query"


def _failure_code_log(caplog: pytest.LogCaptureFixture) -> str:
    records = [
        record.getMessage()
        for record in caplog.records
        if "failure_codes=" in record.getMessage()
    ]
    assert len(records) == 1, records
    return records[0]


@pytest.mark.asyncio
async def test_blocked_sources_log_every_failure_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two blocked aggregate sources, failing for DIFFERENT reasons."""

    authority = AggregateAuthority()
    base = authority.freshness[authority.aggregate_binding.deployment_source_id]
    degraded = authority.aggregate_binding.model_copy(
        update={"source_id": "facts_degraded"}
    )
    denied = authority.aggregate_binding.model_copy(
        update={"source_id": "facts_denied", "required_permissions": ("metrics:admin",)}
    )
    authority.freshness["facts_degraded"] = base.model_copy(
        update={"source_id": "facts_degraded", "status": "stale"}
    )
    authority.freshness["facts_denied"] = base.model_copy(
        update={"source_id": "facts_denied"}
    )
    compiler = authority.compiler(bindings=(degraded, denied))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(PlanStepError) as failure:
            await compiler.compile(authority.plan(), authority.context)
    # The single-code contract is preserved for every caller...
    assert failure.value.code == "metric_aggregate_freshness_denied"
    # ...and the code that used to be dropped is now operator-visible.
    assert _failure_code_log(caplog).endswith(
        "failure_codes=['metric_aggregate_freshness_denied', 'metric_permission_denied']"
    )


@pytest.mark.asyncio
async def test_rejected_detail_source_is_logged_alongside_the_aggregate_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The aggregate fallback is allowed, and the detail source ALSO fails."""

    authority = AggregateAuthority()
    source = authority.aggregate_binding.deployment_source_id
    authority.freshness[source] = authority.freshness[source].model_copy(
        update={"status": "stale"}
    )
    authority.aggregate_binding = authority.aggregate_binding.model_copy(
        update={"allow_detail_fallback": True}
    )
    detail = authority.binding.model_copy(
        update={"required_permissions": ("metrics:admin",)}
    )
    compiler = authority.compiler(bindings=(authority.aggregate_binding, detail))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(PlanStepError) as failure:
            await compiler.compile(authority.plan(), authority.context)
    assert failure.value.code == "metric_aggregate_freshness_denied"
    assert _failure_code_log(caplog).endswith(
        "failure_codes=['metric_aggregate_freshness_denied', 'metric_permission_denied']"
    )
