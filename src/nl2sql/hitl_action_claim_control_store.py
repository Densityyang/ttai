"""Durable owner-scoped HITL action claims in Control PostgreSQL."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.database import DatabasePurpose, create_runtime_async_engine
from src.core.settings import get_settings
from src.nl2sql.hitl_action_claims import (
    HITLActionClaimConflict,
    HITLActionClaimInput,
    HITLActionClaimResult,
    HITLActionClaimStoreUnavailable,
    HITLActionResponseSummary,
)

_SELECT_CLAIM = """
    SELECT owner_user_id, thread_id, pending_version, idempotency_key_sha256,
           payload_sha256, state, response_summary, response_sha256
    FROM hitl_action_claims
    WHERE owner_user_id = :owner_user_id
      AND thread_id = :thread_id
      AND pending_version = :pending_version
    FOR UPDATE
"""

_LOOKUP_CLAIM = """
    SELECT owner_user_id, thread_id, pending_version, idempotency_key_sha256,
           payload_sha256, state, response_summary, response_sha256
    FROM hitl_action_claims
    WHERE owner_user_id = :owner_user_id
      AND thread_id = :thread_id
      AND pending_version = :pending_version
"""

_INSERT_CLAIM = """
    INSERT INTO hitl_action_claims (
      owner_user_id, thread_id, pending_version, idempotency_key_sha256,
      payload_sha256, state, created_at, updated_at
    ) VALUES (
      :owner_user_id, :thread_id, :pending_version, :idempotency_key_sha256,
      :payload_sha256, 'claimed', now(), now()
    )
    ON CONFLICT (owner_user_id, thread_id, pending_version) DO NOTHING
    RETURNING pending_version
"""

_COMPLETE_CLAIM = """
    UPDATE hitl_action_claims
    SET state = 'completed',
        response_summary = CAST(:response_summary AS jsonb),
        response_sha256 = :response_sha256,
        updated_at = now()
    WHERE owner_user_id = :owner_user_id
      AND thread_id = :thread_id
      AND pending_version = :pending_version
      AND idempotency_key_sha256 = :idempotency_key_sha256
      AND payload_sha256 = :payload_sha256
      AND state = 'claimed'
    RETURNING pending_version
"""


class HITLActionClaimIntegrityError(RuntimeError):
    """A persisted claim does not match its checksummed safe response."""

    def __init__(self) -> None:
        super().__init__("hitl_action_claim_row_incoherent")


def _claim_values(claim: HITLActionClaimInput) -> dict[str, object]:
    return {
        "owner_user_id": claim.owner_user_id,
        "thread_id": str(claim.thread_id),
        "pending_version": claim.pending_version,
        "idempotency_key_sha256": claim.idempotency_key_sha256,
        "payload_sha256": claim.payload_sha256,
    }


def _response_json(response: HITLActionResponseSummary) -> str:
    return json.dumps(
        response.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _response_sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_response(row: Mapping[Any, Any]) -> HITLActionResponseSummary:
    try:
        raw = row["response_summary"]
        decoded = dict(raw) if isinstance(raw, Mapping) else json.loads(str(raw))
        response = HITLActionResponseSummary.model_validate(decoded, strict=True)
        canonical = _response_json(response)
        if _response_sha256(canonical) != str(row["response_sha256"]):
            raise ValueError("response_checksum_mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise HITLActionClaimIntegrityError() from exc
    return response


class ControlHITLActionClaimStore:
    """Atomic claim and completion storage on the non-business Control database."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if (database_url is None) == (engine is None):
            raise ValueError("provide exactly one of database_url or engine")
        if engine is not None:
            self._engine = engine
        else:
            self._engine = create_runtime_async_engine(
                database_url or "",
                purpose=DatabasePurpose.CONTROL_APP,
                application_name="ttai-hitl-action-claims",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        try:
            async with self._engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT 1 FROM schema_migrations "
                        "WHERE version = '009_hitl_action_claims'"
                    )
                )
                if result.scalar_one_or_none() is None:
                    raise HITLActionClaimStoreUnavailable()
        except HITLActionClaimStoreUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise HITLActionClaimStoreUnavailable() from exc

    async def lookup(
        self, *, claim: HITLActionClaimInput
    ) -> HITLActionClaimResult | None:
        values = _claim_values(claim)
        try:
            async with self._engine.connect() as connection:
                selected = await connection.execute(text(_LOOKUP_CLAIM), values)
                row = selected.mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise HITLActionClaimStoreUnavailable() from exc
        if row is None:
            return None
        if (
            str(row["idempotency_key_sha256"]) != claim.idempotency_key_sha256
            or str(row["payload_sha256"]) != claim.payload_sha256
        ):
            return HITLActionClaimResult(outcome="conflict")
        if str(row["state"]) == "claimed":
            return HITLActionClaimResult(outcome="in_progress")
        if str(row["state"]) != "completed":
            raise HITLActionClaimIntegrityError()
        return HITLActionClaimResult(outcome="replay", response=_decode_response(row))

    async def claim(
        self, *, claim: HITLActionClaimInput
    ) -> HITLActionClaimResult:
        values = _claim_values(claim)
        try:
            async with self._engine.begin() as connection:
                inserted = await connection.execute(text(_INSERT_CLAIM), values)
                if inserted.scalar_one_or_none() is not None:
                    return HITLActionClaimResult(outcome="claimed")
                selected = await connection.execute(text(_SELECT_CLAIM), values)
                row = selected.mappings().one_or_none()
                if row is None:
                    raise HITLActionClaimStoreUnavailable()
                if (
                    str(row["idempotency_key_sha256"])
                    != claim.idempotency_key_sha256
                    or str(row["payload_sha256"]) != claim.payload_sha256
                ):
                    return HITLActionClaimResult(outcome="conflict")
                if str(row["state"]) == "claimed":
                    return HITLActionClaimResult(outcome="in_progress")
                if str(row["state"]) != "completed":
                    raise HITLActionClaimIntegrityError()
                return HITLActionClaimResult(
                    outcome="replay", response=_decode_response(row)
                )
        except (HITLActionClaimStoreUnavailable, HITLActionClaimIntegrityError):
            raise
        except SQLAlchemyError as exc:
            raise HITLActionClaimStoreUnavailable() from exc

    async def complete(
        self,
        *,
        claim: HITLActionClaimInput,
        response: HITLActionResponseSummary,
    ) -> None:
        values = _claim_values(claim)
        response_payload = _response_json(response)
        response_checksum = _response_sha256(response_payload)
        try:
            async with self._engine.begin() as connection:
                completed = await connection.execute(
                    text(_COMPLETE_CLAIM),
                    {
                        **values,
                        "response_summary": response_payload,
                        "response_sha256": response_checksum,
                    },
                )
                if completed.scalar_one_or_none() is not None:
                    return
                selected = await connection.execute(text(_SELECT_CLAIM), values)
                row = selected.mappings().one_or_none()
                if row is None:
                    raise HITLActionClaimConflict()
                if (
                    str(row["idempotency_key_sha256"])
                    != claim.idempotency_key_sha256
                    or str(row["payload_sha256"]) != claim.payload_sha256
                    or str(row["state"]) != "completed"
                    or _decode_response(row) != response
                ):
                    raise HITLActionClaimConflict()
        except (HITLActionClaimConflict, HITLActionClaimIntegrityError):
            raise
        except SQLAlchemyError as exc:
            raise HITLActionClaimStoreUnavailable() from exc


__all__ = ["ControlHITLActionClaimStore", "HITLActionClaimIntegrityError"]
