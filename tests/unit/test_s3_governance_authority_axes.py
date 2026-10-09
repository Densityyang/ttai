"""S3: the Governance and Authority axes of a Custom Definition.

Every assertion here CONSTRUCTS or SERIALIZES a real model and observes the
result; there is no source-string or field-existence-only evidence.

Frozen A4 requirements exercised:
* Governance and Authority are INDEPENDENT axes, not a linear lifecycle chain.
* The custom ORIGINAL OBJECT invariant is noncanonical, and an in-place
  canonicalize is refused with a typed error.
* CONFIRMED + SAVED + GOVERNANCE_CANDIDATE + noncanonical is legal and
  round-trips.
* No aggregate status can collapse the axes.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from pydantic import ValidationError

from src.nl2sql.artifacts.api_definitions import CreateDefinitionRequest
from src.nl2sql.artifacts.custom_definition import (
    CUSTOM_DEFINITION_CANONICAL_AUTHORITY_ERROR,
    CustomDefinition,
    CustomDefinitionAuthorityViolation,
    DefinitionAxes,
    DefinitionVersion,
    reject_in_place_canonicalization,
    utcnow,
)
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
)
from src.nl2sql.supervisor.schemas import DefinitionBlock, serialize_response_blocks

AXIS_FIELDS = (
    "confirmation",
    "retention",
    "publication",
    "certification",
    "governance",
    "authority",
)
DEFINITION_ID = "def_" + "a" * 32


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.s3.axes",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key="demo.revenue"
            ),
        ),
        unit="count",
    )


def _definition(axes: DefinitionAxes) -> CustomDefinition:
    return CustomDefinition(
        definition_id=DEFINITION_ID,
        owner_user_id="alice",
        axes=axes,
        current_version=DefinitionVersion(
            definition_id=DEFINITION_ID,
            version=1,
            calculation=_spec(),
            title="T",
            created_at=utcnow(),
        ),
    )


def _block(**overrides: Any) -> DefinitionBlock:
    payload: dict[str, Any] = {
        "definition_id": DEFINITION_ID,
        "version": 1,
        "title": "T",
        "confirmation": "DRAFT",
        "retention": "SESSION",
        "publication": "UNPUBLISHED",
        "certification": "UNCERTIFIED",
        "semantic_closed": False,
        "checksum": "b" * 64,
    }
    payload.update(overrides)
    return DefinitionBlock(**payload)


def _legal_combinations() -> list[dict[str, str]]:
    legal: list[dict[str, str]] = []
    for confirmation, retention, publication, certification, governance in (
        itertools.product(
            ("DRAFT", "CONFIRMED"),
            ("SESSION", "SAVED"),
            ("UNPUBLISHED", "PUBLISHED"),
            ("UNCERTIFIED", "CERTIFIED"),
            ("NONE", "GOVERNANCE_CANDIDATE", "UNDER_REVIEW"),
        )
    ):
        combo = {
            "confirmation": confirmation,
            "retention": retention,
            "publication": publication,
            "certification": certification,
            "governance": governance,
            "authority": "noncanonical",
        }
        try:
            DefinitionAxes(**combo)
        except ValidationError:
            continue
        legal.append(combo)
    return legal


# --- (1) representable, serializable, deserializable -------------------------


def test_confirmed_saved_governance_candidate_noncanonical_round_trips() -> None:
    axes = DefinitionAxes(
        confirmation="CONFIRMED",
        retention="SAVED",
        publication="UNPUBLISHED",
        certification="UNCERTIFIED",
        governance="GOVERNANCE_CANDIDATE",
        authority="noncanonical",
    )
    dumped = axes.model_dump(mode="json")
    assert dumped == {
        "confirmation": "CONFIRMED",
        "retention": "SAVED",
        "publication": "UNPUBLISHED",
        "certification": "UNCERTIFIED",
        "governance": "GOVERNANCE_CANDIDATE",
        "authority": "noncanonical",
    }
    assert DefinitionAxes.model_validate(dumped) == axes

    definition = _definition(axes)
    payload = definition.model_dump(mode="json")
    assert payload["axes"]["governance"] == "GOVERNANCE_CANDIDATE"
    assert payload["axes"]["authority"] == "noncanonical"
    restored = CustomDefinition.model_validate(definition.model_dump())
    assert restored == definition
    assert restored.axes == axes
    # a real JSON-text round-trip through the same model
    from_json = CustomDefinition.model_validate_json(definition.model_dump_json())
    assert from_json.axes.governance == "GOVERNANCE_CANDIDATE"
    assert from_json.axes.authority == "noncanonical"


def test_store_shaped_axes_row_defaults_the_two_new_axes() -> None:
    # The four-column store shape (as read from a pre-S3 row) still validates,
    # and the two new server-owned axes take their defaults rather than leaking
    # an absent value.
    axes = DefinitionAxes.model_validate(
        {
            "confirmation": "CONFIRMED",
            "retention": "SAVED",
            "publication": "UNPUBLISHED",
            "certification": "UNCERTIFIED",
        }
    )
    assert axes.governance == "NONE"
    assert axes.authority == "noncanonical"


# --- (2) in-place canonicalize is refused, with a typed error ----------------


def test_in_place_canonicalize_is_refused_with_a_typed_error() -> None:
    # 1. direct construction raises the typed error itself
    with pytest.raises(CustomDefinitionAuthorityViolation) as excinfo:
        DefinitionAxes(authority="canonical")
    assert excinfo.value.code == CUSTOM_DEFINITION_CANONICAL_AUTHORITY_ERROR

    # 2. the deserialization path a store/JSON reader would use still refuses,
    #    and the typed error is recoverable from the ValidationError context
    with pytest.raises(ValidationError) as excinfo2:
        DefinitionAxes.model_validate(
            {
                "confirmation": "DRAFT",
                "retention": "SESSION",
                "publication": "UNPUBLISHED",
                "certification": "UNCERTIFIED",
                "governance": "NONE",
                "authority": "canonical",
            }
        )
    assert isinstance(
        excinfo2.value.errors()[0]["ctx"]["error"],
        CustomDefinitionAuthorityViolation,
    )

    # 3. the single guard itself
    with pytest.raises(CustomDefinitionAuthorityViolation):
        reject_in_place_canonicalization("canonical")

    # 4. the in-place rewrite path pydantic's model_copy SKIPS validation for
    axes = DefinitionAxes()
    with pytest.raises(CustomDefinitionAuthorityViolation):
        axes.model_copy(update={"authority": "canonical"})
    # ...while a legal axis copy still works and leaves authority alone
    copied = axes.model_copy(update={"confirmation": "CONFIRMED"})
    assert copied.confirmation == "CONFIRMED"
    assert copied.authority == "noncanonical"

    # 5. refused at EVERY legal state, so no combination can "earn" canonical
    for combo in _legal_combinations():
        state = DefinitionAxes(**combo)
        assert state.authority == "noncanonical"
        with pytest.raises(CustomDefinitionAuthorityViolation):
            state.model_copy(update={"authority": "canonical"})


# --- (3) the axes are pairwise independent -----------------------------------


@pytest.mark.parametrize(
    ("baseline", "field", "new_value"),
    [
        ({"confirmation": "DRAFT"}, "confirmation", "CONFIRMED"),
        ({"confirmation": "CONFIRMED"}, "retention", "SAVED"),
        (
            {"confirmation": "CONFIRMED", "retention": "SAVED"},
            "publication",
            "PUBLISHED",
        ),
        (
            {
                "confirmation": "CONFIRMED",
                "retention": "SAVED",
                "publication": "PUBLISHED",
            },
            "certification",
            "CERTIFIED",
        ),
        ({"confirmation": "DRAFT"}, "governance", "GOVERNANCE_CANDIDATE"),
        ({"confirmation": "DRAFT"}, "governance", "UNDER_REVIEW"),
        (
            {
                "confirmation": "CONFIRMED",
                "retention": "SAVED",
                "publication": "PUBLISHED",
                "certification": "CERTIFIED",
            },
            "governance",
            "GOVERNANCE_CANDIDATE",
        ),
    ],
)
def test_changing_one_axis_leaves_every_other_axis_untouched(
    baseline: dict[str, str], field: str, new_value: str
) -> None:
    base = DefinitionAxes(**baseline)
    changed = DefinitionAxes(**{**baseline, field: new_value})
    assert getattr(changed, field) == new_value
    for other in AXIS_FIELDS:
        if other == field:
            continue
        assert getattr(changed, other) == getattr(base, other), (
            field + " leaked into " + other
        )


def test_governance_candidate_neither_requires_nor_grants_publication() -> None:
    candidate = DefinitionAxes(governance="GOVERNANCE_CANDIDATE")
    assert candidate.confirmation == "DRAFT"
    assert candidate.retention == "SESSION"
    assert candidate.publication == "UNPUBLISHED"
    assert candidate.certification == "UNCERTIFIED"

    published = DefinitionAxes(
        confirmation="CONFIRMED", retention="SAVED", publication="PUBLISHED"
    )
    assert published.governance == "NONE"

    certified = DefinitionAxes(
        confirmation="CONFIRMED",
        retention="SAVED",
        publication="PUBLISHED",
        certification="CERTIFIED",
    )
    assert certified.governance == "NONE"
    assert certified.authority == "noncanonical"


# --- (4) there is no aggregate status ----------------------------------------


def test_no_single_status_field_can_distinguish_all_legal_combinations() -> None:
    combinations = _legal_combinations()
    assert len(combinations) == 15, "unexpected legal combination count"
    assert "status" not in DefinitionAxes.model_fields
    assert "state" not in DefinitionAxes.model_fields
    for field in AXIS_FIELDS:
        distinct = {combo[field] for combo in combinations}
        assert len(distinct) < len(combinations), (
            field + " is a de-facto aggregate status"
        )

    # Two distinct states differing ONLY in governance: no single status can
    # encode both.
    first = DefinitionAxes(governance="NONE")
    second = DefinitionAxes(governance="GOVERNANCE_CANDIDATE")
    assert first.model_dump() != second.model_dump()


# --- (5) pre-existing behaviour is unchanged ---------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retention": "SAVED"},
        {"publication": "PUBLISHED"},
        {"confirmation": "CONFIRMED", "publication": "PUBLISHED"},
        {"certification": "CERTIFIED"},
        {
            "confirmation": "CONFIRMED",
            "retention": "SAVED",
            "certification": "CERTIFIED",
        },
    ],
)
def test_existing_implication_invariants_still_reject(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        DefinitionAxes(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"confirmation": "CONFIRMED"},
        {"confirmation": "CONFIRMED", "retention": "SAVED"},
        {"confirmation": "CONFIRMED", "retention": "SAVED", "publication": "PUBLISHED"},
        {
            "confirmation": "CONFIRMED",
            "retention": "SAVED",
            "publication": "PUBLISHED",
            "certification": "CERTIFIED",
        },
    ],
)
def test_existing_legal_states_still_construct(kwargs: dict[str, str]) -> None:
    axes = DefinitionAxes(**kwargs)
    assert axes.governance == "NONE"
    assert axes.authority == "noncanonical"


# --- (6) DefinitionBlock presents both axes ----------------------------------


def test_definition_block_carries_both_new_axes_through_serialization() -> None:
    block = _block(governance="GOVERNANCE_CANDIDATE", authority="noncanonical")
    dumped = block.model_dump()
    assert dumped["governance"] == "GOVERNANCE_CANDIDATE"
    assert dumped["authority"] == "noncanonical"
    assert "status" not in dumped

    serialized = serialize_response_blocks([block])
    assert serialized[0]["type"] == "definition"
    assert serialized[0]["governance"] == "GOVERNANCE_CANDIDATE"
    assert serialized[0]["authority"] == "noncanonical"

    # the dict path re-validates through PUBLIC_BLOCK_MODELS and keeps the axes
    revalidated = serialize_response_blocks(
        [{**dumped, "governance": "UNDER_REVIEW"}]
    )
    assert revalidated[0]["governance"] == "UNDER_REVIEW"
    assert revalidated[0]["authority"] == "noncanonical"

    # the defaults preserve the pre-S3 block shape
    default_block = _block()
    assert default_block.governance == "NONE"
    assert default_block.authority == "noncanonical"
    assert "status" not in DefinitionBlock.model_fields


# --- (7) the two axes are server-owned, never client-injectable --------------


def test_client_cannot_inject_governance_or_authority_axes() -> None:
    spec_payload = _spec().model_dump()
    for injected in ("governance", "authority", "governance_candidate", "canonical"):
        with pytest.raises(ValidationError):
            CalculationSpec(**{**spec_payload, injected: "GOVERNANCE_CANDIDATE"})
    for injected in (
        "governance",
        "authority",
        "axes",
        "confirmation",
        "retention",
        "publication",
        "certification",
    ):
        with pytest.raises(ValidationError):
            CreateDefinitionRequest.model_validate(
                {"title": "T", "calculation": spec_payload, injected: "NONE"}
            )


async def test_service_owns_the_two_axes_and_defaults_them() -> None:
    service = CustomDefinitionService()
    draft = await service.create_draft(
        owner_user_id="alice", title="T", calculation=_spec()
    )
    definition = await service.get_owned_definition(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    assert definition.axes.governance == "NONE"
    assert definition.axes.authority == "noncanonical"
