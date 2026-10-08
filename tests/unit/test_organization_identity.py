"""OrganizationIdentity: org data is CONTEXT, never authorization."""

from __future__ import annotations

import dataclasses
from typing import Any

from src.core.auth.provider import TTApiAuthProvider
from src.core.auth.types import AuthUser, OrganizationIdentity
from src.nl2sql.contracts import AuthorizationContext, RequestIdentity


def _provider() -> TTApiAuthProvider:
    return TTApiAuthProvider.__new__(TTApiAuthProvider)


def _org(payload: dict[str, Any]) -> OrganizationIdentity:
    return _provider()._extract_organization(payload)


def test_profile_maps_every_organization_field() -> None:
    org = _org(
        {
            "company_id": 1,
            "company_name": "C",
            "department_id": 0,
            "department_name": "D",
            "team_id": 12,
            "team_name": "T",
            "employee_id": 99,
            "position_id": 3,
            "position_name": "装机",
        }
    )
    assert org == OrganizationIdentity(
        company_id="1",
        company_name="C",
        department_id="0",
        department_name="D",
        team_id="12",
        team_name="T",
        employee_id="99",
        position_id="3",
        position_name="装机",
    )


def test_ids_normalize_to_opaque_strings() -> None:
    org = _org({"company_id": 1, "team_id": 12, "employee_id": 99, "position_id": 3})
    for value in (org.company_id, org.team_id, org.employee_id, org.position_id):
        assert isinstance(value, str)
    # a falsy-but-present id is a REAL id and must be preserved
    assert _org({"department_id": 0}).department_id == "0"


def test_absent_organization_is_an_empty_identity_not_none() -> None:
    org = _org({"user_id": 7})
    assert org == OrganizationIdentity()
    assert org.is_empty is True


def test_wrong_typed_values_drop_without_raising() -> None:
    org = _org({"company_id": {"nested": 1}, "team_name": 42, "employee_id": True})
    assert org.company_id is None
    assert org.team_name is None
    assert org.employee_id is None


def test_blank_values_collapse_to_none() -> None:
    org = _org({"company_name": "   ", "team_id": "  "})
    assert org.company_name is None
    assert org.team_id is None


def test_pii_never_crosses_the_boundary() -> None:
    payload = {
        "company_id": 1,
        "id_card": "SECRET",
        "phone": "SECRET",
        "emergency_contact": "SECRET",
        "emergency_phone": "SECRET",
        "employee_code": "SECRET",
        "gender": "SECRET",
        "email": "SECRET",
        "avatar": "SECRET",
    }
    org = _org(payload)
    for name in (
        "id_card",
        "phone",
        "emergency_contact",
        "emergency_phone",
        "employee_code",
        "gender",
        "email",
        "avatar",
    ):
        assert not hasattr(org, name)
    # the dataclass field set is EXACTLY the declared identity fields
    assert {f.name for f in dataclasses.fields(OrganizationIdentity)} == {
        "company_id",
        "department_id",
        "team_id",
        "employee_id",
        "position_id",
        "company_name",
        "department_name",
        "team_name",
        "position_name",
    }


def test_organization_is_never_part_of_the_authorization_model() -> None:
    """Organization identity must not leak into identity or authority."""

    # the authoritative models must not carry org membership at all
    assert "organization" not in RequestIdentity.model_fields
    assert "organization" not in AuthorizationContext.model_fields
    # AuthUser carries it as CONTEXT, alongside - never inside - roles/permissions
    assert "organization" in {f.name for f in dataclasses.fields(AuthUser)}
    user = AuthUser(
        user_id="u",
        telephone=None,
        roles=["analyst"],
        permissions=["metrics:read"],
        organization=OrganizationIdentity(team_id="12"),
    )
    assert user.roles == ["analyst"]
    assert user.permissions == ["metrics:read"]
    # being in team 12 adds no permission whatsoever
    assert "12" not in user.permissions
    assert user.roles == ["analyst"]


def test_legacy_construction_still_works() -> None:
    user = AuthUser(user_id="alice", telephone=None, roles=["analyst"], permissions=["*"])
    assert user.organization is None


def test_free_text_position_is_not_aliased_to_position_name() -> None:
    org = _org({"position": "装机工程师"})
    assert org.position_name is None
