"""Product-mode / run-envelope capability contract (contract-only, no I/O).

FROZEN product translation implemented here, not reinterpreted:

* one RUN has exactly one immutable effective ``ProductMode``;
* ``ProductMode`` belongs to the RUN ENVELOPE, never to the conversation;
* comparison, ranking, trend, arithmetic, execution count, Save and SAVED
  identity never determine the mode;
* ``auto`` resolves to ANALYZE and is NOT a classifier;
* switching mode starts a NEW RUN: context MAY migrate, PERMISSION NEVER does;
* the Agent may SUGGEST a switch and may never perform a silent capability
  upgrade;
* ``RouteName`` (fast/standard/deep) is a separate execution/budget axis and is
  deliberately NOT equated with ``ProductMode``.

This module performs no I/O, no persistence and no wiring.  It is additive: the
capability description below is a CONTRACT, and nothing here grants a real
authorization, a canonical identity or a publication right.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.nl2sql.contracts import ProductMode

SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

# Deployment provenance for the AUTHORITY behind a run.  This is deliberately
# NOT a ProductMode: mode is the user's intent, whereas this states which
# authority actually answered.  Presenting "demo" here makes a demo run
# impossible to mistake for a production-authorized one.
AuthorityProvenance = Literal["backend", "demo", "local_real_demo", "unavailable"]

# --- capability vocabulary ----------------------------------------------------
# The capability a mode confers.  These are CAPABILITY names, not permissions:
# holding one never implies data authorization, organization scope, relation
# access, model-input egress, canonical authority or publication authority.
Capability = Literal[
    "deterministic_retrieval",
    "model_analysis",
    "run_scoped_derivation",
    "semantic_authoring",
]

# The requested mode may be "auto", which is NOT a mode: it resolves to ANALYZE.
RequestedMode = Literal["auto", "QUERY", "ANALYZE", "BUILD"]

DEFAULT_MODE_FOR_AUTO: Final[ProductMode] = "ANALYZE"

_MODE_CAPABILITIES: Final[dict[str, tuple[str, ...]]] = {
    # QUERY / Mode1: zero-model authoritative retrieval.
    "QUERY": ("deterministic_retrieval",),
    # ANALYZE / Mode2: model participation + run-scoped noncanonical derivation.
    "ANALYZE": ("deterministic_retrieval", "model_analysis", "run_scoped_derivation"),
    # BUILD / Mode3: semantic authoring/mutation capability.
    "BUILD": (
        "deterministic_retrieval",
        "model_analysis",
        "run_scoped_derivation",
        "semantic_authoring",
    ),
}

# Capabilities a mode must NEVER confer.  Enumerated so the invariant is
# testable rather than merely documented.
FORBIDDEN_CAPABILITIES: Final[frozenset[str]] = frozenset(
    (
        "data_authorization",
        "organization_scope",
        "relation_access",
        "model_input_egress",
        "canonical_authority",
        "publication_authority",
        "code_act",
    )
)


def capabilities_for_mode(mode: ProductMode) -> tuple[Capability, ...]:
    """The bounded capability set conferred by an effective mode."""

    return _MODE_CAPABILITIES[mode]  # type: ignore[return-value]


def resolve_requested_mode(requested: RequestedMode) -> ProductMode:
    """Resolve a requested mode to the immutable effective mode.

    ``auto`` is NOT a classifier: it deterministically resolves to ANALYZE.
    """

    if requested == "auto":
        return DEFAULT_MODE_FOR_AUTO
    return requested


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _checksum(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class RunEnvelope(_StrictFrozenModel):
    """The immutable per-run mode envelope.

    There is exactly ONE effective mode per run and it never changes: a mode
    switch is a NEW RUN.  ``run_id`` identifies this run; a switched run carries
    a new value and MAY carry migrated conversation context.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str = Field(min_length=1, max_length=64)
    requested_mode: RequestedMode
    effective_mode: ProductMode
    # A switch carried over from a previous run.  The previous run identity is
    # retained for lineage; permissions are deliberately NOT part of the envelope.
    switched_from_run_id: str | None = Field(default=None, min_length=1, max_length=64)
    # Conversation context MAY migrate; this names what was carried, never a
    # permission.  Absence means a fresh run.
    migrated_context_ref: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_envelope(self) -> RunEnvelope:
        if self.effective_mode != resolve_requested_mode(self.requested_mode):
            raise ValueError("effective mode must be the resolution of the requested mode")
        if self.switched_from_run_id == self.run_id:
            raise ValueError("a mode switch must start a NEW run")
        if self.switched_from_run_id is None and self.migrated_context_ref is not None:
            raise ValueError("migrated context requires a switch lineage")
        return self

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return capabilities_for_mode(self.effective_mode)

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


class ModeSwitchProposal(_StrictFrozenModel):
    """An Agent SUGGESTION to switch mode.  It is never an execution.

    Accepting a proposal is an explicit user act that starts a NEW run, so the
    proposal deliberately carries no capability and no permission of its own.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    current_run_id: str = Field(min_length=1, max_length=64)
    current_mode: ProductMode
    proposed_mode: ProductMode
    reason: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_proposal(self) -> ModeSwitchProposal:
        if self.current_mode == self.proposed_mode:
            raise ValueError("a mode-switch proposal must change the mode")
        return self

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


class ModeCapabilityOutcome(_StrictFrozenModel):
    """The typed outcome when a mode cannot serve the request.

    QUERY must never silently escalate to a model.  When deterministic
    resolution cannot safely identify the request, the run returns this typed
    outcome so the caller can offer an explicit switch instead.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str = Field(min_length=1, max_length=64)
    effective_mode: ProductMode
    outcome: Literal["resolved", "clarification_required", "cannot_resolve"]
    # Populated only for cannot_resolve: the mode that WOULD be able to serve it.
    suggested_mode: ProductMode | None = None
    unresolved_slots: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_outcome(self) -> ModeCapabilityOutcome:
        if self.outcome == "cannot_resolve" and self.suggested_mode is None:
            raise ValueError("cannot_resolve must name a suggested mode")
        if self.outcome != "cannot_resolve" and self.suggested_mode is not None:
            raise ValueError("suggested mode is only meaningful for cannot_resolve")
        if self.suggested_mode == self.effective_mode:
            raise ValueError("a suggestion must name a different mode")
        if self.outcome == "resolved" and self.unresolved_slots:
            raise ValueError("a resolved outcome cannot carry unresolved slots")
        if self.outcome == "clarification_required" and not self.unresolved_slots:
            raise ValueError("clarification requires an unresolved slot")
        return self

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


def mode_capability_registry() -> dict[str, tuple[str, ...]]:
    """Expose the frozen mode -> capability map for verification."""

    return {mode: tuple(caps) for mode, caps in _MODE_CAPABILITIES.items()}


__all__ = [
    "DEFAULT_MODE_FOR_AUTO",
    "AuthorityProvenance",
    "FORBIDDEN_CAPABILITIES",
    "SCHEMA_VERSION",
    "Capability",
    "ModeCapabilityOutcome",
    "ModeSwitchProposal",
    "RequestedMode",
    "RunEnvelope",
    "capabilities_for_mode",
    "mode_capability_registry",
    "resolve_requested_mode",
]
