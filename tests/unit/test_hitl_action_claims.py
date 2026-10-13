from __future__ import annotations

import asyncio
from uuid import UUID

from src.nl2sql.hitl_action_claims import (
    HITLActionClaimInput,
    HITLActionResponseSummary,
    InMemoryHITLActionClaimStore,
    hitl_action_payload_checksum,
    hitl_idempotency_key_checksum,
)

_THREAD = UUID("11111111-1111-1111-1111-111111111111")


def _claim(
    *,
    owner: str = "alice",
    version: int = 1,
    key: str = "once",
    payload: str = "approve",
) -> HITLActionClaimInput:
    return HITLActionClaimInput(
        owner_user_id=owner,
        thread_id=_THREAD,
        pending_version=version,
        idempotency_key_sha256=hitl_idempotency_key_checksum(key),
        payload_sha256=hitl_action_payload_checksum({"action": payload}),
    )


def _response() -> HITLActionResponseSummary:
    return HITLActionResponseSummary(
        target_type="typed_decision",
        status="recorded",
        version=1,
        decision_status="resolved_pending_revalidation",
        continuation_status="current_authorization_denied",
    )


async def test_atomic_claim_has_one_winner_and_replays_safe_completion() -> None:
    store = InMemoryHITLActionClaimStore()
    claim = _claim()

    results = await asyncio.gather(*(store.claim(claim=claim) for _ in range(12)))
    assert [result.outcome for result in results].count("claimed") == 1
    assert [result.outcome for result in results].count("in_progress") == 11

    await store.complete(claim=claim, response=_response())
    replay = await store.lookup(claim=claim)
    assert replay is not None
    assert replay.outcome == "replay"
    assert replay.response == _response()


async def test_claim_conflicts_are_owner_and_version_scoped() -> None:
    store = InMemoryHITLActionClaimStore()
    original = _claim()
    assert (await store.claim(claim=original)).outcome == "claimed"

    assert (await store.lookup(claim=_claim(key="different"))).outcome == "conflict"
    assert (
        await store.lookup(claim=_claim(payload="reject"))
    ).outcome == "conflict"
    assert (await store.claim(claim=_claim(owner="bob"))).outcome == "claimed"
    assert (await store.claim(claim=_claim(version=2))).outcome == "claimed"
