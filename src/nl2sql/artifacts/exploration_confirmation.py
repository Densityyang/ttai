"""Run-scoped EXPLORATION confirmation: never a definition confirmation.

An exploration confirmation records that a caller confirmed the RESULT OF ONE
EXPLORATORY RUN.  It is run-scoped and ephemeral by construction, and it is a
DIFFERENT object from the definition's business confirmation (A4/A6):

* it never sets ``DefinitionAxes.confirmation`` to CONFIRMED;
* it never creates, modifies or re-versions a Custom Definition;
* it carries no authority, no canonicality and no lifecycle axis;
* it may hold a READ-ONLY provenance link to the draft that was explored.

The invariant is explicit on the object itself:
``replaces_definition_confirmation`` is the literal ``False``, and the service
exposes NO method that calls a mutating definition operation.  Both claims are
pinned by tests that count definition-store writes, not by this docstring.

The definition dependency is typed as the NARROW read-only protocol
``DefinitionExactVersionReader`` (a single owner-scoped ``get_exact_version``),
not as the full ``CustomDefinitionService``: the exploration surface is
structurally incapable of naming a definition mutation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable, Final, Literal, Protocol, cast
from uuid import uuid4

from pydantic import ConfigDict, Field, ValidationError

from src.nl2sql.contracts import StrictContract

if TYPE_CHECKING:
    from src.nl2sql.artifacts.custom_definition import DefinitionVersion

EXPLORATION_CONFIRMATION_SCHEMA_VERSION: Final = "1.0"

# The stable error code of a missing exploration confirmation.
EXPLORATION_CONFIRMATION_NOT_FOUND: Final[str] = "exploration_confirmation_not_recorded"

# The stable error code of a client attempt to own the exploration actor/time or
# the server-minted exploration identity.
EXPLORATION_IDENTITY_IS_SERVER_OWNED: Final[str] = (
    "exploration_identity_is_server_owned"
)

# The stable error code of a payload that is not a valid exploration request at
# all (a missing required field, a malformed run id, ...).
EXPLORATION_CONFIRMATION_INVALID: Final[str] = "exploration_confirmation_invalid"

_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"
_DEFINITION_ID_PATTERN = r"^def_[0-9a-f]{32}$"
_RUN_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"

# Every spelling of "who / when / which id" a client could try to inject.  The
# strict contract's extra="forbid" already refuses all of them; this explicit set
# is defense-in-depth that yields the STABLE typed code instead of a generic
# validation error.  NOTE: ``subject`` is deliberately ABSENT - it is a real
# client field of THIS request (unlike the definition-confirmation sibling).
_FORBIDDEN_EXPLORATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "confirmed_by",
        "confirmed_at",
        "confirmer",
        "recorded_by",
        "actor",
        "identity",
        "user_id",
        "owner_user_id",
        "run_owner_user_id",
        "exploration_id",
        "timestamp",
        "created_at",
        "updated_at",
        "schema_version",
        "authority",
        "canonical",
        "canonicality",
        "permission",
        "permissions",
        "role",
        "roles",
        "confirmed",
        "replaces_definition_confirmation",
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_exploration_id() -> str:
    return "exp_" + uuid4().hex


class ExplorationConfirmationNotFound(LookupError):
    """Typed refusal: no exploration confirmation exists for that run/id."""

    def __init__(self) -> None:
        super().__init__(EXPLORATION_CONFIRMATION_NOT_FOUND)


class ExplorationIdentityInjection(ValidationError):
    """Typed refusal: a client tried to supply a server-owned exploration field.

    It deliberately SUBCLASSES pydantic's ``ValidationError`` so every caller
    that already catches a validation failure keeps working, while the TYPE
    itself (and its stable ``code``) is the product-visible refusal.  The route
    layer maps the TYPE - never the message text - onto ONE stable error code.
    """

    code: Final[str] = EXPLORATION_IDENTITY_IS_SERVER_OWNED

    @property
    def fields(self) -> tuple[str, ...]:
        """The injected field names, taken from the typed pydantic errors."""

        return tuple(str(error["loc"][0]) for error in self.errors())


def _identity_injection(fields: tuple[str, ...]) -> ExplorationIdentityInjection:
    """Build the typed refusal as a REAL pydantic ValidationError.

    ``from_exception_data`` is the only supported constructor for the Rust
    ``ValidationError`` base, so the typed error is produced through it; the
    subclass keeps the stable ``code`` and the ``fields`` accessor.
    """

    return cast(
        ExplorationIdentityInjection,
        ExplorationIdentityInjection.from_exception_data(
            "ExploreConfirmationRequest",
            [
                {
                    "type": "extra_forbidden",
                    "loc": (field,),
                    "input": "forged",
                }
                for field in fields
            ],
        ),
    )


class DefinitionExactVersionReader(Protocol):
    """The ONLY definition capability an exploration confirmation may hold.

    It is exactly one owner-scoped READ.  The exploration service is typed
    against this protocol (not the full definition service), so it cannot name
    ``create_draft`` / ``confirm`` / ``save`` / ``create_revision`` /
    ``mark_semantic_closed`` or any other mutating operation at all.
    """

    async def get_exact_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> DefinitionVersion: ...


class ReadOnlyDefinitionReader:
    """A RUNTIME read-only adapter over a definition reader.

    It exposes EXACTLY ONE owner-scoped read and nothing else, so the
    exploration service holds no attribute through which a definition mutation
    (``create_draft`` / ``confirm`` / ``save`` / ``create_revision`` /
    ``mark_semantic_closed`` / ``project_published`` / ``project_certified``)
    could even be NAMED.  The exploration service wraps whatever it is given in
    this adapter, so the read-only guarantee holds by construction rather than by
    convention.
    """

    __slots__ = ("_definitions",)

    def __init__(self, definitions: DefinitionExactVersionReader) -> None:
        self._definitions = definitions

    async def get_exact_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> DefinitionVersion:
        return await self._definitions.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )


class ExplorationDefinitionReference(StrictContract):
    """A READ-ONLY provenance link to the draft an exploration observed.

    It records the OBSERVED state (including closure) and is never an input to
    any definition mutation: the exploration service cannot write it back.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    definition_id: str = Field(pattern=_DEFINITION_ID_PATTERN)
    version: int = Field(ge=1)
    definition_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    semantic_closed: bool


class ExploreConfirmationRequest(StrictContract):
    """The COMPLETE client-visible shape of an exploration confirmation.

    ``extra="forbid"`` (StrictContract) refuses every authority / lifecycle /
    canonical / confirmation field, and there is deliberately no
    ``confirmed_by`` / ``confirmed_at``: the actor and the time are server-owned.
    """

    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    subject: str = Field(min_length=1, max_length=512)
    # An OPTIONAL read-only reference to the draft that was explored.
    definition_id: str | None = Field(default=None, pattern=_DEFINITION_ID_PATTERN)
    version: int | None = Field(default=None, ge=1)


class ExplorationConfirmation(StrictContract):
    """ONE run-scoped exploration confirmation.  Never a definition confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    exploration_id: str = Field(pattern=r"^exp_[0-9a-f]{32}$")
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    subject: str = Field(min_length=1, max_length=512)
    # SERVER-OWNED: the authenticated identity, never a client field.
    confirmed_by: str = Field(min_length=1, max_length=256)
    # SERVER-OWNED: the service clock.
    confirmed_at: datetime
    definition_reference: ExplorationDefinitionReference | None = None
    # EXPLICIT invariant: an exploration confirmation never stands in for the
    # definition's business confirmation.
    replaces_definition_confirmation: Literal[False] = False


def parse_explore_confirmation_request(
    payload: Mapping[str, Any],
) -> ExploreConfirmationRequest:
    """Parse the ONE accepted exploration payload, refusing identity injection.

    The explicit forbidden-field check runs FIRST so the refusal is a directly
    catchable typed error carrying the stable code, not a generic validation
    error; any OTHER unknown field is still refused by ``extra="forbid"``.
    """

    forbidden = tuple(
        field for field in sorted(_FORBIDDEN_EXPLORATION_FIELDS) if field in payload
    )
    if forbidden:
        raise _identity_injection(forbidden)
    return ExploreConfirmationRequest.model_validate(dict(payload))


class ExplorationConfirmationStore(Protocol):
    """Queryable storage for run-scoped exploration confirmations."""

    async def put(self, *, record: ExplorationConfirmation) -> None: ...

    async def get(
        self, *, run_id: str, exploration_id: str
    ) -> ExplorationConfirmation | None: ...

    async def list_for_run(
        self, *, run_id: str
    ) -> tuple[ExplorationConfirmation, ...]: ...


class InMemoryExplorationConfirmationStore:
    """Process-local, run-keyed, deliberately non-durable default."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ExplorationConfirmation] = {}

    async def ping(self) -> None:
        """Readiness probe.  A process-local store is trivially reachable."""

    async def close(self) -> None:
        """Nothing to release."""

    async def put(self, *, record: ExplorationConfirmation) -> None:
        self._records[(record.run_id, record.exploration_id)] = record

    async def get(
        self, *, run_id: str, exploration_id: str
    ) -> ExplorationConfirmation | None:
        return self._records.get((run_id, exploration_id))

    async def list_for_run(
        self, *, run_id: str
    ) -> tuple[ExplorationConfirmation, ...]:
        return tuple(
            record
            for (candidate_run_id, _exploration_id), record in self._records.items()
            if candidate_run_id == run_id
        )


class ExplorationConfirmationService:
    """Record run-scoped exploration confirmations; NEVER touch definition lifecycle.

    The definition dependency is injected READ-ONLY - typed as the narrow
    ``DefinitionExactVersionReader`` protocol - for an owner-scoped resolution of
    the reference, and this class deliberately exposes NO method that calls a
    mutating definition operation, so an exploration confirmation can never move
    ``DefinitionAxes.confirmation``, ``semantic_closed`` or the version boundary.
    """

    def __init__(
        self,
        *,
        definitions: DefinitionExactVersionReader | None = None,
        store: ExplorationConfirmationStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # ALWAYS wrapped in the runtime read-only adapter: even a caller that
        # injected the FULL definition service cannot reach a mutation through
        # this object.
        self._definitions: DefinitionExactVersionReader | None = (
            None if definitions is None else ReadOnlyDefinitionReader(definitions)
        )
        self._store: ExplorationConfirmationStore = (
            store if store is not None else InMemoryExplorationConfirmationStore()
        )
        self._clock = clock or _utcnow

    @property
    def definition_reader(self) -> DefinitionExactVersionReader | None:
        """The runtime READ-ONLY definition reader this service holds.

        It is ALWAYS a ``ReadOnlyDefinitionReader`` (or None), never the raw
        definition service, so a caller can verify the read-only guarantee
        without reaching into a private attribute.
        """

        return self._definitions

    async def confirm_exploration(
        self,
        *,
        owner_user_id: str,
        run_id: str,
        subject: str,
        definition_id: str | None = None,
        version: int | None = None,
    ) -> ExplorationConfirmation:
        """Record an exploration confirmation.  It writes NO definition state."""

        if not owner_user_id or not owner_user_id.strip():
            raise ValueError("exploration confirmation requires a server identity")
        reference: ExplorationDefinitionReference | None = None
        if definition_id is not None or version is not None:
            if definition_id is None or version is None or self._definitions is None:
                raise ValueError(
                    "an exploration reference requires definition_id, version and "
                    "a definition service"
                )
            # READ-ONLY owner-scoped resolution: the SAME not-found as an absent
            # definition, and no mutation of any kind.
            exact = await self._definitions.get_exact_version(
                owner_user_id=owner_user_id,
                definition_id=definition_id,
                version=version,
            )
            reference = ExplorationDefinitionReference(
                definition_id=exact.definition_id,
                version=exact.version,
                definition_checksum=exact.checksum,
                semantic_closed=exact.semantic_closed,
            )
        record = ExplorationConfirmation(
            exploration_id=new_exploration_id(),
            run_id=run_id,
            subject=subject,
            confirmed_by=owner_user_id,
            confirmed_at=self._clock(),
            definition_reference=reference,
        )
        await self._store.put(record=record)
        return record

    async def confirm_from_client_payload(
        self, *, owner_user_id: str, payload: Mapping[str, Any]
    ) -> ExplorationConfirmation:
        """The strict seam a route uses: the payload is parsed BEFORE any write.

        A client that tries to inject ``confirmed_by`` / ``confirmed_at`` /
        ``exploration_id`` (or any other server-owned spelling) is refused with
        the stable typed code and leaves ZERO state change.
        """

        request = parse_explore_confirmation_request(payload)
        return await self.confirm_exploration(
            owner_user_id=owner_user_id,
            run_id=request.run_id,
            subject=request.subject,
            definition_id=request.definition_id,
            version=request.version,
        )

    async def get_exploration_confirmation(
        self, *, run_id: str, exploration_id: str
    ) -> ExplorationConfirmation:
        record = await self._store.get(run_id=run_id, exploration_id=exploration_id)
        if record is None:
            raise ExplorationConfirmationNotFound()
        return record

    async def get_owned_exploration_confirmation(
        self, *, owner_user_id: str, run_id: str, exploration_id: str
    ) -> ExplorationConfirmation:
        """The OWNER-SCOPED reader: a foreign record is the SAME not-found.

        The owner is the server-recorded ``confirmed_by``; a foreign caller is
        therefore indistinguishable from a never-existing id and this route is
        never an existence oracle.
        """

        record = await self._store.get(run_id=run_id, exploration_id=exploration_id)
        if record is None or record.confirmed_by != owner_user_id:
            raise ExplorationConfirmationNotFound()
        return record

    async def list_for_run(
        self, *, run_id: str
    ) -> tuple[ExplorationConfirmation, ...]:
        return await self._store.list_for_run(run_id=run_id)

    async def list_owned_for_run(
        self, *, owner_user_id: str, run_id: str
    ) -> tuple[ExplorationConfirmation, ...]:
        """Every confirmation THIS OWNER recorded for that run, in store order.

        A run id is client-supplied, so the owner filter is what keeps another
        caller's run invisible: a foreign run yields an EMPTY list, never a
        partial disclosure.
        """

        records = await self._store.list_for_run(run_id=run_id)
        return tuple(
            record for record in records if record.confirmed_by == owner_user_id
        )


__all__ = [
    "EXPLORATION_CONFIRMATION_INVALID",
    "EXPLORATION_CONFIRMATION_NOT_FOUND",
    "EXPLORATION_CONFIRMATION_SCHEMA_VERSION",
    "EXPLORATION_IDENTITY_IS_SERVER_OWNED",
    "DefinitionExactVersionReader",
    "ExploreConfirmationRequest",
    "ExplorationConfirmation",
    "ExplorationConfirmationNotFound",
    "ExplorationConfirmationService",
    "ExplorationConfirmationStore",
    "ExplorationDefinitionReference",
    "ExplorationIdentityInjection",
    "InMemoryExplorationConfirmationStore",
    "ReadOnlyDefinitionReader",
    "new_exploration_id",
    "parse_explore_confirmation_request",
]
