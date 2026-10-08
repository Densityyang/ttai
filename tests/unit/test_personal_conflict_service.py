from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.nl2sql.artifacts.custom_definition import (
    DefinitionVersion,
    DefinitionVersionLifecycle,
    derive_parameter_contract,
)
from src.nl2sql.artifacts.publication import PublishedSemanticPackage, PublishedVersion
from src.nl2sql.semantic.calculation_contract import (
    BinaryOperand,
    CalculationInputSpec,
    CalculationSpec,
    InputRefOperand,
    LiteralOperand,
)
from src.nl2sql.semantic.personal_conflict_contract import PersonalSelection
from src.nl2sql.semantic.personal_conflict_service import (
    PersonalConflictServiceError,
    project_installed_published_candidate,
    project_own_saved_candidate,
    resolve_personal_candidate_conflict,
    validate_personal_selection,
)


def _spec(*, multiplier: str) -> CalculationSpec:
    return CalculationSpec(
        calculation_id="custom.archive_weighted",
        expression=BinaryOperand(
            op="multiply",
            left=InputRefOperand(role="actual"),
            right=LiteralOperand(value=Decimal(multiplier)),
        ),
        inputs=(
            CalculationInputSpec(
                role="actual",
                provenance="published_gold",
                metric_key="repair_service_archive_rate_overall_day",
            ),
        ),
        unit="percent",
        precision=2,
        rounding="half_up",
    )


def _own_candidate():
    spec = _spec(multiplier="1")
    version = DefinitionVersion(
        definition_id="def_" + "1" * 32,
        version=3,
        calculation=spec,
        parameter_contract=derive_parameter_contract(spec),
        title="Archive performance",
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    return project_own_saved_candidate(
        version=version,
        lifecycle=DefinitionVersionLifecycle(
            confirmation="CONFIRMED", retention="SAVED"
        ),
        owner_user_id="user-1",
        certification_state="uncertified",
        star_count=0,
    )


def _installed_candidate():
    spec = _spec(multiplier="2")
    published = PublishedVersion(
        identity_id="published.archive.performance",
        version=7,
        title="Archive performance",
        owner_user_id="publisher-1",
        owner_label="Published owner",
        source_label="Published catalogue",
        definition_checksum="b" * 64,
        published_at="2026-09-20T00:00:00Z",
        semantic=PublishedSemanticPackage(
            calculation=spec,
            parameter_contract=derive_parameter_contract(spec),
            source_definition_id="def_" + "2" * 32,
            source_definition_version=7,
            source_definition_checksum="b" * 64,
        ),
    )
    return project_installed_published_candidate(
        published=published,
        certification_state="certified",
        star_count=999,
    )


def test_materially_distinct_own_and_installed_candidates_require_clarification() -> None:
    own = _own_candidate()
    installed = _installed_candidate()

    projection = resolve_personal_candidate_conflict((own, installed))

    assert projection.resolution.outcome == "clarification_required"
    assert projection.semantic_conflict is not None
    comparison = projection.conflict_comparison
    assert comparison is not None
    assert len(comparison.candidates) == 2
    assert {
        candidate.origin for candidate in comparison.candidates
    } == {"own", "installed"}
    assert any(
        difference.kind == "formula_version_lineage"
        for difference in projection.semantic_conflict.differences
    )
    serialized = projection.model_dump(mode="json")
    assert "winner" not in serialized
    assert "recommended_identity" not in serialized


def test_star_and_certification_are_descriptive_only_and_never_select() -> None:
    projection = resolve_personal_candidate_conflict(
        (_own_candidate(), _installed_candidate())
    )

    assert projection.resolution.outcome == "clarification_required"
    assert projection.semantic_conflict is not None
    assert not hasattr(projection.semantic_conflict, "winner")
    comparison = projection.conflict_comparison
    assert comparison is not None
    installed = next(
        item
        for item in comparison.candidates
        if item.origin == "installed"
    )
    assert installed.star_count == 999
    assert installed.certification_state == "certified"


def test_same_title_does_not_hide_actual_semantic_difference() -> None:
    own = _own_candidate()
    installed = _installed_candidate()

    assert own.reference.display_name == installed.reference.display_name
    assert own.semantic_checksum != installed.semantic_checksum
    projection = resolve_personal_candidate_conflict((own, installed))
    assert projection.resolution.outcome == "clarification_required"


def test_same_identity_with_different_semantics_fails_before_deduplication() -> None:
    own = _own_candidate()
    installed = _installed_candidate()
    conflicting_identity = installed.model_copy(
        update={"reference": own.reference, "lineage": own.lineage}
    )

    with pytest.raises(
        PersonalConflictServiceError,
        match="personal_candidate_identity_semantic_mismatch",
    ):
        resolve_personal_candidate_conflict((own, conflicting_identity))


def test_selection_defaults_run_scoped_and_must_reference_exact_conflict() -> None:
    projection = resolve_personal_candidate_conflict(
        (_own_candidate(), _installed_candidate())
    )
    assert projection.semantic_conflict is not None
    selected = projection.semantic_conflict.candidates[0]
    selection = PersonalSelection(
        conflict_id=projection.semantic_conflict.conflict_id,
        required_slot=projection.semantic_conflict.required_slot,
        selected_candidate_id=selected.candidate_id,
    )

    assert selection.selection_scope == "run_scoped"
    assert validate_personal_selection(selection, projection=projection) is selection

    wrong = selection.model_copy(update={"selected_candidate_id": "not-in-conflict"})
    with pytest.raises(
        PersonalConflictServiceError, match="personal_selection_candidate_mismatch"
    ):
        validate_personal_selection(wrong, projection=projection)


def test_own_candidate_requires_exact_saved_confirmed_lifecycle() -> None:
    spec = _spec(multiplier="1")
    version = DefinitionVersion(
        definition_id="def_" + "3" * 32,
        version=1,
        calculation=spec,
        parameter_contract=derive_parameter_contract(spec),
        title="Draft candidate",
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
    )

    with pytest.raises(
        PersonalConflictServiceError, match="personal_candidate_not_saved_confirmed"
    ):
        project_own_saved_candidate(
            version=version,
            lifecycle=DefinitionVersionLifecycle(
                confirmation="CONFIRMED", retention="SESSION"
            ),
            owner_user_id="user-1",
        )


def test_installed_candidate_without_semantic_package_fails_closed() -> None:
    legacy = PublishedVersion(
        identity_id="legacy.display.only",
        version=1,
        title="Legacy display only",
        owner_user_id="publisher-1",
        owner_label="Publisher",
        source_label="Legacy catalogue",
        definition_checksum="c" * 64,
        published_at="2026-09-20T00:00:00Z",
    )

    with pytest.raises(
        PersonalConflictServiceError, match="personal_candidate_semantics_unavailable"
    ):
        project_installed_published_candidate(
            published=legacy,
            certification_state="unknown",
            star_count=0,
        )
