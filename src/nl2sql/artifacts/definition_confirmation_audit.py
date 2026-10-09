"""Server-owned audit records for a Custom Definition business confirmation.

A confirmation is a BUSINESS transition (A4/A6): the exact definition version
becomes CONFIRMED and therefore reusable.  The lifecycle axes record THAT it
happened; they do not record WHO confirmed it, WHEN, or WHICH server-side
decision/validation authorised it.  This module supplies exactly that missing
audit record, as a SEPARATE server-owned object.

Three invariants are deliberate and each one is pinned by an execution-level
test rather than by a docstring:

* the record is SERVER-OWNED.  The actor is the authenticated owner identity and
  the timestamp is the service clock; a client payload that tries to inject
  ``confirmed_by`` / ``confirmed_at`` (or any other identity/time spelling) is
  refused by the strict request contract BEFORE any lifecycle transition runs.
* the record is NOT definition semantics.  It lives in its own store and is never
  a field of ``DefinitionVersion`` / ``CustomDefinition``, so adding it cannot
  move any definition checksum by even one bit.
* the record is QUERYABLE through the service, so "who confirmed this exact
  version, when, against which decision reference" is answerable.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal, Protocol

from pydantic import ConfigDict, Field

from src.nl2sql.contracts import StrictContract

CONFIRMATION_AUDIT_SCHEMA_VERSION: Final = "1.0"

# The stable error code of a client attempt to own the confirmation actor/time.
CONFIRMATION_IDENTITY_IS_SERVER_OWNED: Final[str] = (
    "confirmation_identity_is_server_owned"
)
# The stable error code of a missing confirmation audit record.
CONFIRMATION_RECORD_NOT_FOUND: Final[str] = "definition_confirmation_not_recorded"

_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"
_DEFINITION_ID_PATTERN = r"^def_[0-9a-f]{32}$"

# Every spelling of "who/when" a client could try to inject.  The strict
# contract's extra="forbid" already refuses all of them; this explicit set is
# defense-in-depth that yields the STABLE typed code (and an adversarial,
# self-describing message) instead of a generic validation error.
_FORBIDDEN_CONFIRMATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "confirmed_by",
        "confirmed_at",
        "confirmer",
        "actor",
        "identity",
        "user_id",
        "owner_user_id",
        "subject",
        "timestamp",
        "confirmed",
    }
)


class ConfirmationIdentityInjection(ValueError):
    """Typed refusal: a client tried to supply the confirmation actor/time."""

    code: Final[str] = CONFIRMATION_IDENTITY_IS_SERVER_OWNED

    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        super().__init__(CONFIRMATION_IDENTITY_IS_SERVER_OWNED + ": " + ",".join(fields))


class ConfirmationRecordNotFound(LookupError):
    """Typed refusal: no confirmation audit record exists for that version."""

    def __init__(self) -> None:
        super().__init__(CONFIRMATION_RECORD_NOT_FOUND)


class ConfirmDefinitionRequest(StrictContract):
    """The COMPLETE client-visible shape of a definition confirmation request.

    There is deliberately NO ``confirmed_by`` / ``confirmed_at`` (and no other
    identity/time) field: the actor comes from the authenticated request context
    and the timestamp from the service clock.  ``StrictContract`` sets
    ``extra="forbid"``, so every injected field is a typed refusal.
    """

    # The ONLY optional client input: a bounded reference to the server-side
    # decision/validation the caller believes authorises this confirmation (for
    # example a HITL request id).  It is recorded as provenance; it grants
    # nothing and it can never move the definition lifecycle by itself.
    decision_reference: str | None = Field(default=None, max_length=256)


class ConfirmationRecord(StrictContract):
    """ONE server-owned confirmation audit record.  Never definition semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    definition_id: str = Field(pattern=_DEFINITION_ID_PATTERN)
    version: int = Field(ge=1)
    # The EXACT checksum of the version that was confirmed: the audit record is
    # bound to the confirmed identity, not to a mutable "current" pointer.
    definition_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    # SERVER-OWNED: the authenticated owner identity, never a client field.
    confirmed_by: str = Field(min_length=1, max_length=256)
    # SERVER-OWNED: the service clock at the confirmation transition.
    confirmed_at: datetime
    # A bounded, server-owned binding to the decision/validation that authorised
    # this confirmation.  Optional: a confirmation with no reference is still
    # auditable, and the reference can never be the source of authority.
    decision_reference: str | None = Field(default=None, max_length=256)


def parse_confirm_definition_request(
    payload: Mapping[str, Any],
) -> ConfirmDefinitionRequest:
    """Parse the ONE accepted confirmation payload, refusing identity injection.

    The explicit forbidden-field check runs FIRST so the refusal is a directly
    catchable typed error carrying the stable code, not a generic validation
    error; any OTHER unknown field is still refused by ``extra="forbid"``.
    """

    forbidden = tuple(
        field for field in sorted(_FORBIDDEN_CONFIRMATION_FIELDS) if field in payload
    )
    if forbidden:
        raise ConfirmationIdentityInjection(forbidden)
    return ConfirmDefinitionRequest.model_validate(dict(payload))


class ConfirmationAuditStore(Protocol):
    """Queryable, append-only storage for confirmation audit records."""

    async def put(self, *, record: ConfirmationRecord) -> None: ...

    async def get(
        self, *, definition_id: str, version: int
    ) -> ConfirmationRecord | None: ...

    async def list_for_definition(
        self, *, definition_id: str
    ) -> tuple[ConfirmationRecord, ...]: ...


class InMemoryConfirmationAuditStore:
    """Process-local, append-only, deliberately non-durable default.

    Append-only on purpose: a repeated confirmation of the same version appends
    ANOTHER record instead of erasing the previous one, so the audit trail can
    never be rewritten by a later confirmation.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, int], list[ConfirmationRecord]] = {}

    async def ping(self) -> None:
        """Readiness probe.  A process-local store is trivially reachable."""

    async def close(self) -> None:
        """Nothing to release."""

    async def put(self, *, record: ConfirmationRecord) -> None:
        key = (record.definition_id, record.version)
        self._records.setdefault(key, []).append(record)

    async def get(
        self, *, definition_id: str, version: int
    ) -> ConfirmationRecord | None:
        records = self._records.get((definition_id, version))
        return None if not records else records[-1]

    async def list_for_definition(
        self, *, definition_id: str
    ) -> tuple[ConfirmationRecord, ...]:
        return tuple(
            record
            for (candidate_id, _version), records in self._records.items()
            if candidate_id == definition_id
            for record in records
        )


__all__ = [
    "CONFIRMATION_AUDIT_SCHEMA_VERSION",
    "CONFIRMATION_IDENTITY_IS_SERVER_OWNED",
    "CONFIRMATION_RECORD_NOT_FOUND",
    "ConfirmDefinitionRequest",
    "ConfirmationAuditStore",
    "ConfirmationIdentityInjection",
    "ConfirmationRecord",
    "ConfirmationRecordNotFound",
    "InMemoryConfirmationAuditStore",
    "parse_confirm_definition_request",
]
