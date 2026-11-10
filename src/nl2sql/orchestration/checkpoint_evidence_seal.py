"""Server-verifiable integrity seal for grounded execution checkpoints."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from src.nl2sql.contracts import (
    AnswerArtifact,
    CheckpointEvidenceSeal,
    ExecutionPlan,
    PlanExecutionRecord,
    QueryPlan,
)
from src.nl2sql.orchestration.evidence_digest import canonical_json_digest

_DOMAIN = b"tt-ai:grounded-execution-checkpoint:v1\0"


class CheckpointEvidenceSealError(ValueError):
    """Stable failure for absent, invalid, or unverifiable checkpoint seals."""


def seal_grounded_execution_checkpoint(
    *,
    key: str | bytes,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    execution_record: PlanExecutionRecord,
    answer_artifact: AnswerArtifact,
    answer_text: str,
    unit_authority_identities: Mapping[str, str],
) -> PlanExecutionRecord:
    """Return a record sealed over all checkpoint evidence and server authority."""

    if execution_record.status != "succeeded":
        raise CheckpointEvidenceSealError("checkpoint_evidence_execution_not_succeeded")
    if execution_record.evidence_seal is not None:
        raise CheckpointEvidenceSealError("checkpoint_evidence_already_sealed")
    secret = _key_bytes(key)
    digest = _checkpoint_payload_digest(
        query_plan=query_plan,
        execution_plan=execution_plan,
        execution_record=execution_record,
        answer_artifact=answer_artifact,
        answer_text=answer_text,
        unit_authority_identities=unit_authority_identities,
    )
    signature = hmac.new(secret, _DOMAIN + digest.encode("ascii"), hashlib.sha256).hexdigest()
    return execution_record.model_copy(
        update={
            "evidence_seal": CheckpointEvidenceSeal(
                key_id=_key_id(secret),
                signature=signature,
            )
        }
    )


def verify_grounded_execution_checkpoint(
    *,
    key: str | bytes | None,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    execution_record: PlanExecutionRecord,
    answer_artifact: AnswerArtifact,
    answer_text: str,
    unit_authority_identities: Mapping[str, str],
    required: bool = True,
) -> None:
    """Verify a checkpoint seal before any grounded fact is accepted."""

    seal = execution_record.evidence_seal
    if seal is None:
        if required:
            raise CheckpointEvidenceSealError("checkpoint_evidence_seal_missing")
        return
    if key is None:
        raise CheckpointEvidenceSealError("checkpoint_evidence_seal_key_unavailable")
    secret = _key_bytes(key)
    if not hmac.compare_digest(seal.key_id, _key_id(secret)):
        raise CheckpointEvidenceSealError("checkpoint_evidence_seal_key_mismatch")
    digest = _checkpoint_payload_digest(
        query_plan=query_plan,
        execution_plan=execution_plan,
        execution_record=execution_record,
        answer_artifact=answer_artifact,
        answer_text=answer_text,
        unit_authority_identities=unit_authority_identities,
    )
    expected = hmac.new(
        secret,
        _DOMAIN + digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(seal.signature, expected):
        raise CheckpointEvidenceSealError("checkpoint_evidence_seal_invalid")


def _checkpoint_payload_digest(
    *,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    execution_record: PlanExecutionRecord,
    answer_artifact: AnswerArtifact,
    answer_text: str,
    unit_authority_identities: Mapping[str, str],
) -> str:
    if execution_plan.query_plan_sha256 != query_plan.checksum:
        raise CheckpointEvidenceSealError("checkpoint_evidence_plan_mismatch")
    record_payload = execution_record.model_dump(mode="json", exclude={"evidence_seal"})
    # PlanExecutionRecord contains the complete receipt/source/snapshot and
    # semantic-validation evidence. These explicit plan checksums prevent
    # cross-plan replay even if a future record shape omits one of them.
    payload = {
        "version": "v1",
        "query_plan_sha256": query_plan.checksum,
        "execution_plan_sha256": execution_plan.checksum,
        "execution_record": record_payload,
        "answer_artifact": answer_artifact.model_dump(mode="json"),
        "answer_text": answer_text,
        "unit_authority_identities": {
            step_id: unit_authority_identities[step_id]
            for step_id in sorted(unit_authority_identities)
        },
    }
    return canonical_json_digest(payload)


def _key_bytes(key: str | bytes) -> bytes:
    secret = key.encode("utf-8") if isinstance(key, str) else key
    if len(secret) < 32:
        raise CheckpointEvidenceSealError("checkpoint_evidence_seal_key_too_short")
    return secret


def _key_id(secret: bytes) -> str:
    return hashlib.sha256(b"tt-ai:checkpoint-evidence-key-id:v1\0" + secret).hexdigest()[:16]
