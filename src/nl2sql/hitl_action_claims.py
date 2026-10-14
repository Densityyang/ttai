"""Owner-scoped durable claims for one-shot HITL actions.

The Control-PG implementation is the production boundary. The in-memory
implementation exists only for local/dev tests and deliberately does not claim
restart durability.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from src.nl2sql.contracts import StrictContract

_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")

HITLContinuationStatus = Literal[
    "resolved_pending_revalidation",
    "resolved_pending_current_authorization",
    "resolved_continuing_current_authorization",
    "current_authorization_unavailable",
    "current_authorization_denied",
    "continuation_execution_plan_denied",
    "continuation_stopped",
    "revalidation_denied",
    "awaiting_decision",
    "choose_recorded_no_continuation",
    "rejected",
    "cancelled",
    "invalid",
    "metric_plan_correction_rejected",
]


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def hitl_action_payload_checksum(payload: Mapping[str, object]) -> str:
    """Hash a canonical request projection without retaining decision text."""

    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def hitl_idempotency_key_checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class HITLActionResponseSummary(StrictContract):
    """Small, safe response projection persisted for exact idempotent replay."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target_type: Literal["legacy_hitl", "typed_decision"] | None = None
    status: Literal["approved", "modified", "rejected", "cancelled", "recorded"]
    version: int = Field(ge=1)
    decision_status: Literal[
        "resolved_pending_revalidation",
        "choose_recorded_no_continuation",
        "rejected",
        "cancelled",
    ] | None = None
    continuation_status: HITLContinuationStatus | None = None

    @model_validator(mode="after")
    def _response_shape_is_coherent(self) -> "HITLActionResponseSummary":
        if self.target_type == "typed_decision":
            if self.status != "recorded" or self.decision_status is None:
                raise ValueError("typed_hitl_response_incoherent")
        elif (
            self.status == "recorded"
            or self.decision_status is not None
            or self.continuation_status is not None
        ):
            raise ValueError("legacy_hitl_response_incoherent")
        return self


class HITLActionClaimInput(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    owner_user_id: str = Field(min_length=1, max_length=256)
    thread_id: UUID
    pending_version: int = Field(ge=1)
    idempotency_key_sha256: str = Field(pattern=_CHECKSUM.pattern)
    payload_sha256: str = Field(pattern=_CHECKSUM.pattern)


class HITLActionClaimResult(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Literal["claimed", "in_progress", "replay", "conflict"]
    response: HITLActionResponseSummary | None = None

    @model_validator(mode="after")
    def _result_shape_is_coherent(self) -> "HITLActionClaimResult":
        if (self.outcome == "replay") != (self.response is not None):
            raise ValueError("hitl_claim_result_incoherent")
        return self


class HITLActionClaimConflict(RuntimeError):
    """A pending HITL version is already bound to a different request."""

    code = "hitl_action_claim_conflict"

    def __init__(self) -> None:
        super().__init__(self.code)


class HITLActionClaimStoreUnavailable(RuntimeError):
    """The configured claim store cannot establish an atomic claim safely."""

    code = "hitl_action_claim_store_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


class HITLActionClaimStore(Protocol):
    async def ping(self) -> None: ...

    async def close(self) -> None: ...

    async def lookup(
        self, *, claim: HITLActionClaimInput
    ) -> HITLActionClaimResult | None: ...

    async def claim(self, *, claim: HITLActionClaimInput) -> HITLActionClaimResult: ...

    async def complete(
        self,
        *,
        claim: HITLActionClaimInput,
        response: HITLActionResponseSummary,
    ) -> None: ...


class InMemoryHITLActionClaimStore:
    """Process-local implementation for tests and non-product development only."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._claims: dict[
            tuple[str, UUID, int],
            tuple[str, str, HITLActionResponseSummary | None],
        ] = {}

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def lookup(
        self, *, claim: HITLActionClaimInput
    ) -> HITLActionClaimResult | None:
        identity = (claim.owner_user_id, claim.thread_id, claim.pending_version)
        async with self._lock:
            existing = self._claims.get(identity)
            if existing is None:
                return None
            stored_key, stored_payload, response = existing
            if (
                stored_key != claim.idempotency_key_sha256
                or stored_payload != claim.payload_sha256
            ):
                return HITLActionClaimResult(outcome="conflict")
            if response is None:
                return HITLActionClaimResult(outcome="in_progress")
            return HITLActionClaimResult(outcome="replay", response=response)

    async def claim(self, *, claim: HITLActionClaimInput) -> HITLActionClaimResult:
        identity = (claim.owner_user_id, claim.thread_id, claim.pending_version)
        async with self._lock:
            existing = self._claims.get(identity)
            if existing is None:
                self._claims[identity] = (
                    claim.idempotency_key_sha256,
                    claim.payload_sha256,
                    None,
                )
                return HITLActionClaimResult(outcome="claimed")
            stored_key, stored_payload, response = existing
            if (
                stored_key != claim.idempotency_key_sha256
                or stored_payload != claim.payload_sha256
            ):
                return HITLActionClaimResult(outcome="conflict")
            if response is None:
                return HITLActionClaimResult(outcome="in_progress")
            return HITLActionClaimResult(outcome="replay", response=response)

    async def complete(
        self,
        *,
        claim: HITLActionClaimInput,
        response: HITLActionResponseSummary,
    ) -> None:
        identity = (claim.owner_user_id, claim.thread_id, claim.pending_version)
        async with self._lock:
            existing = self._claims.get(identity)
            if existing is None:
                raise HITLActionClaimConflict()
            stored_key, stored_payload, stored_response = existing
            if (
                stored_key != claim.idempotency_key_sha256
                or stored_payload != claim.payload_sha256
            ):
                raise HITLActionClaimConflict()
            if stored_response is not None and stored_response != response:
                raise HITLActionClaimConflict()
            self._claims[identity] = (stored_key, stored_payload, response)


__all__ = [
    "HITLActionClaimConflict",
    "HITLActionClaimInput",
    "HITLActionClaimResult",
    "HITLActionClaimStore",
    "HITLActionClaimStoreUnavailable",
    "HITLActionResponseSummary",
    "HITLContinuationStatus",
    "InMemoryHITLActionClaimStore",
    "hitl_action_payload_checksum",
    "hitl_idempotency_key_checksum",
]
