"""Presentation layer: noncanonical AD_HOC provenance without weakening canonical."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.nl2sql.supervisor.schemas import (
    ProvenanceAuthorityBlock,
    ProvenanceBlock,
    ProvenanceTimeRangeBlock,
    serialize_response_blocks,
)

DERIVED = "adhoc_" + "c" * 32


def _authority() -> ProvenanceAuthorityBlock:
    return ProvenanceAuthorityBlock(
        execution_plan_checksum="b" * 64,
        receipt_step_ids=("calculate_adhoc",),
    )


def _base() -> dict[str, object]:
    return {
        "evidence_checksum": "a" * 64,
        "analysis_window": ProvenanceTimeRangeBlock(
            start="2026-08-01", end="2026-08-31", timezone="Asia/Shanghai"
        ),
        "fact_ids": ("fact-1",),
        "authority_provenance": _authority(),
    }


def test_pure_ad_hoc_provenance_is_representable_with_actual_sources() -> None:
    block = ProvenanceBlock(
        calculation_scope="ad_hoc_noncanonical",
        derived_output_ids=(DERIVED,),
        metric_keys=("metric.revenue", "metric.stores"),
        **_base(),  # type: ignore[arg-type]
    )
    assert block.calculation_scope == "ad_hoc_noncanonical"
    assert block.derived_output_ids == (DERIVED,)
    # The actual source metrics are preserved for the product label, but the
    # derived output is NOT one of them.
    assert block.metric_keys == ("metric.revenue", "metric.stores")
    assert DERIVED not in block.metric_keys
    payload = block.model_dump(mode="json")
    assert payload["calculation_scope"] == "ad_hoc_noncanonical"
    assert payload["derived_output_ids"] == [DERIVED]
    assert payload["metric_keys"] == ["metric.revenue", "metric.stores"]


def test_pure_ad_hoc_provenance_needs_no_metric_key() -> None:
    block = ProvenanceBlock(
        calculation_scope="ad_hoc_noncanonical",
        derived_output_ids=(DERIVED,),
        **_base(),  # type: ignore[arg-type]
    )
    assert block.metric_keys == ()
    assert block.derived_output_ids == (DERIVED,)


def test_canonical_provenance_still_requires_a_metric_key() -> None:
    # The pre-existing invariant is UNCHANGED: canonical provenance (scope
    # absent or "canonical") must name at least one metric key.
    with pytest.raises(ValidationError):
        ProvenanceBlock(**_base())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ProvenanceBlock(calculation_scope="canonical", **_base())  # type: ignore[arg-type]


def test_canonical_provenance_rejects_derived_output_ids() -> None:
    with pytest.raises(ValidationError):
        ProvenanceBlock(
            metric_keys=("metric.revenue",),
            derived_output_ids=(DERIVED,),
            **_base(),  # type: ignore[arg-type]
        )


def test_noncanonical_provenance_requires_a_derived_output_id() -> None:
    with pytest.raises(ValidationError):
        ProvenanceBlock(
            calculation_scope="ad_hoc_noncanonical",
            metric_keys=("metric.revenue",),
            **_base(),  # type: ignore[arg-type]
        )


def test_fact_ids_remain_required() -> None:
    with pytest.raises(ValidationError):
        ProvenanceBlock(
            calculation_scope="ad_hoc_noncanonical",
            derived_output_ids=(DERIVED,),
            evidence_checksum="a" * 64,
            analysis_window=ProvenanceTimeRangeBlock(
                start="2026-08-01", end="2026-08-31", timezone="Asia/Shanghai"
            ),
            authority_provenance=_authority(),
        )


def test_wire_serialization_keeps_canonical_strict_and_noncanonical_labeled() -> None:
    noncanonical = {
        "type": "provenance",
        "calculation_scope": "ad_hoc_noncanonical",
        "derived_output_ids": [DERIVED],
        "metric_keys": ["metric.revenue"],
        **_base(),
    }
    serialized = serialize_response_blocks([noncanonical])
    assert serialized[0]["calculation_scope"] == "ad_hoc_noncanonical"
    assert serialized[0]["derived_output_ids"] == (DERIVED,)

    # A canonical provenance without any metric key is still rejected on the wire.
    canonical_empty = {
        "type": "provenance",
        **_base(),
    }
    with pytest.raises(ValidationError):
        serialize_response_blocks([canonical_empty])
