"""A6 material-semantic axes: pure diff, deterministic order, no authority."""

from __future__ import annotations

import pytest

from src.nl2sql.artifacts.definition_semantics import (
    AXIS_ORDER,
    MATERIAL_AXES,
    DefinitionSemantics,
    SemanticDeclaration,
    is_material_change,
    semantic_axes,
)

_EXPR = "a" * 64
_EXPR2 = "b" * 64
_CONTRACT = "c" * 64
_CONTRACT2 = "d" * 64


def _declaration(
    *,
    expression: str | None = _EXPR,
    contract: str | None = _CONTRACT,
    semantics: DefinitionSemantics | None = None,
) -> SemanticDeclaration:
    return SemanticDeclaration(
        expression_checksum=expression,
        parameter_contract_checksum=contract,
        semantics=semantics,
    )


def test_identical_declarations_have_no_axis() -> None:
    left = _declaration(semantics=DefinitionSemantics(population="paid orders"))
    right = _declaration(semantics=DefinitionSemantics(population="paid orders"))
    assert semantic_axes(left, right) == ()
    assert is_material_change(left, right) is False


def test_absent_declaration_is_not_material() -> None:
    assert semantic_axes(None, None) == ()
    assert is_material_change(None, None) is False


def test_expression_change_is_one_axis() -> None:
    assert semantic_axes(_declaration(), _declaration(expression=_EXPR2)) == (
        "expression",
    )


def test_parameter_contract_change_is_one_axis() -> None:
    assert semantic_axes(_declaration(), _declaration(contract=_CONTRACT2)) == (
        "parameter_contract",
    )


@pytest.mark.parametrize(
    ("before", "after", "axis"),
    [
        (
            DefinitionSemantics(population="all orders"),
            DefinitionSemantics(population="paid orders"),
            "population",
        ),
        (
            DefinitionSemantics(denominator="all stores"),
            DefinitionSemantics(denominator="open stores"),
            "numerator_denominator",
        ),
        (
            DefinitionSemantics(numerator="revenue"),
            DefinitionSemantics(numerator="gross revenue"),
            "numerator_denominator",
        ),
        (
            DefinitionSemantics(deduplication="none"),
            DefinitionSemantics(deduplication="distinct_key"),
            "deduplication",
        ),
        (
            DefinitionSemantics(grain="store"),
            DefinitionSemantics(grain="region"),
            "grain",
        ),
        (
            DefinitionSemantics(scope="team-1"),
            DefinitionSemantics(scope="team-2"),
            "scope",
        ),
        (
            DefinitionSemantics(time_semantics="calendar month"),
            DefinitionSemantics(time_semantics="business month"),
            "time_semantics",
        ),
        (
            DefinitionSemantics(join_semantics="inner on store_id"),
            DefinitionSemantics(join_semantics="left on store_id"),
            "join_semantics",
        ),
        (
            DefinitionSemantics(null_semantics="exclude"),
            DefinitionSemantics(null_semantics="include"),
            "null_semantics",
        ),
        (
            DefinitionSemantics(unit_precision="yuan"),
            DefinitionSemantics(unit_precision="wan yuan"),
            "unit_precision",
        ),
        (
            DefinitionSemantics(provenance="manual"),
            DefinitionSemantics(provenance="automatic base table"),
            "provenance",
        ),
    ],
)
def test_each_declared_axis_is_detected(
    before: DefinitionSemantics, after: DefinitionSemantics, axis: str
) -> None:
    assert semantic_axes(
        _declaration(semantics=before), _declaration(semantics=after)
    ) == (axis,)


def test_first_declaration_of_an_axis_is_a_change() -> None:
    # None means "declares nothing", so declaring it for the first time IS a
    # material change rather than an absence of information.
    assert semantic_axes(
        _declaration(semantics=DefinitionSemantics()),
        _declaration(semantics=DefinitionSemantics(population="paid orders")),
    ) == ("population",)


def test_declaration_to_absent_is_a_change() -> None:
    assert semantic_axes(
        _declaration(semantics=DefinitionSemantics(population="paid orders")),
        _declaration(semantics=DefinitionSemantics()),
    ) == ("population",)


def test_many_changes_are_reported_in_the_frozen_order() -> None:
    before = _declaration(
        semantics=DefinitionSemantics(
            population="all orders",
            deduplication="none",
            unit_precision="yuan",
        )
    )
    after = _declaration(
        expression=_EXPR2,
        semantics=DefinitionSemantics(
            population="paid orders",
            deduplication="distinct_key",
            unit_precision="wan yuan",
        ),
    )
    axes = semantic_axes(before, after)
    assert axes == ("expression", "population", "deduplication", "unit_precision")
    # The reporting order is the frozen order, not insertion order.
    assert list(axes) == [axis for axis in AXIS_ORDER if axis in set(axes)]


def test_diff_is_pure_and_repeatable() -> None:
    before = _declaration(semantics=DefinitionSemantics(population="all orders"))
    after = _declaration(semantics=DefinitionSemantics(population="paid orders"))
    snapshot = (before.model_dump(mode="json"), after.model_dump(mode="json"))
    first = semantic_axes(before, after)
    second = semantic_axes(before, after)
    assert first == second
    assert (before.model_dump(mode="json"), after.model_dump(mode="json")) == snapshot


def test_axes_lists_only_what_is_declared() -> None:
    assert DefinitionSemantics().axes() == ()
    assert DefinitionSemantics(population="x").axes() == ("population",)
    assert DefinitionSemantics(
        unit_precision="yuan", population="x"
    ).axes() == ("population", "unit_precision")


def test_declaration_checksum_is_stable_and_sensitive() -> None:
    base = _declaration(semantics=DefinitionSemantics(population="x"))
    same = _declaration(semantics=DefinitionSemantics(population="x"))
    other = _declaration(semantics=DefinitionSemantics(population="y"))
    assert base.checksum == same.checksum
    assert base.checksum != other.checksum
    assert len(base.checksum) == 64


def test_authority_fields_are_refused() -> None:
    # A9: a shared semantic representation is never a shared authority.
    for forbidden in ("canonical", "authority", "metric_key", "permission"):
        with pytest.raises(Exception):
            SemanticDeclaration.model_validate(
                {"expression_checksum": _EXPR, forbidden: "x"}
            )


def test_material_axes_covers_the_frozen_order() -> None:
    assert MATERIAL_AXES == frozenset(AXIS_ORDER)
    assert len(AXIS_ORDER) == len(set(AXIS_ORDER))
