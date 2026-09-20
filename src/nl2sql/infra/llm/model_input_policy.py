"""Approved-destination and technical-secret gate applied before model egress.

P2-S2 owns exactly two questions for ONE resolved target: is the destination
(provider AND model) in the versioned policy, and does the payload carry a
technical secret?  It is not a second authorization system and not a business
DLP matrix: ordinary authorised business content remains available to an
approved model.

The gate is evaluated independently for the primary and for the fallback inside
ModelGateway._invoke_target, so a permitted primary never implies a permitted
fallback.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from src.nl2sql.contracts import ModelInputDecision, ModelRequest, PolicyLifecycle
from src.nl2sql.infra.llm.profiles import ModelTarget
from src.nl2sql.observability.content_policy import (
    deployment_secret_values,
    scan_value,
)

MODEL_INPUT_POLICY_VERSION = "model-input.bootstrap.v1"
MODEL_INPUT_POLICY_CALIBRATED_VERSION = "model-input.v1"

MODEL_TARGET_NOT_APPROVED = "model_target_not_approved"
MODEL_INPUT_SECRET_DETECTED = "model_input_secret_detected"


class ModelInputPolicyUncalibrated(RuntimeError):
    """A production deployment would run an uncalibrated or empty policy."""


@dataclass(frozen=True)
class ApprovedDestination:
    """One EXACT approved (provider, model) pair.

    Approval is pair-scoped, never a provider x model cross-product: approving
    (a, x) and (b, y) must NOT approve (a, y) or (b, x).  The type is frozen so
    the approved set is an immutable, hashable identity that can be checksummed.
    """

    provider: str
    model: str


@dataclass(frozen=True)
class ModelInputPolicy:
    """Versioned EXACT-destination allowlist plus technical-secret denial.

    A destination is the immutable (provider, model) PAIR, so the same provider
    set and the same model set with a different pairing are different policies.
    The configured secret values are the deployment's own credentials held for
    Layer-1 substring matching.  They are excluded from repr AND from the
    checksum, so the replayable fingerprint describes the policy, not secrets.
    """

    version: str
    state: PolicyLifecycle
    approved_destinations: frozenset[ApprovedDestination]
    secret_values: tuple[str, ...] = field(default=(), repr=False, compare=False)

    @property
    def checksum(self) -> str:
        return _policy_checksum(self)

    def evaluate(self, target: ModelTarget, request: ModelRequest) -> ModelInputDecision:
        """Decide one target independently; never mutates and never raises."""

        content_sha256 = _content_sha256(request)
        if _destination(target) not in self.approved_destinations:
            return _decision(
                self,
                target,
                outcome="deny",
                reason=MODEL_TARGET_NOT_APPROVED,
                matched_categories=(),
                content_sha256=content_sha256,
            )
        # R10: a policy may ADD secret values, but the deployment's complete
        # bounded secret source is ALWAYS included, even when this policy was
        # constructed with an empty/partial explicit tuple.
        findings = scan_value(
            {"messages": request.messages, "tool_schema": request.tool_schema},
            secret_values=deployment_secret_values(self.secret_values),
        )
        if findings:
            categories = tuple(sorted({finding.category for finding in findings}))
            return _decision(
                self,
                target,
                outcome="deny",
                reason=MODEL_INPUT_SECRET_DETECTED,
                matched_categories=categories,
                content_sha256=content_sha256,
            )
        return _decision(
            self,
            target,
            outcome="allow",
            reason=None,
            matched_categories=(),
            content_sha256=content_sha256,
        )

    def require_production_ready(self) -> None:
        """Fail closed for a bootstrap or destination-empty production policy."""

        if self.state == "bootstrap":
            raise ModelInputPolicyUncalibrated(
                "model input policy is still in the bootstrap state"
            )
        if not self.approved_destinations:
            raise ModelInputPolicyUncalibrated(
                "model input policy has no approved destinations"
            )


def bootstrap_model_input_policy(
    targets: Iterable[ModelTarget],
    *,
    secret_values: Iterable[str] = (),
) -> ModelInputPolicy:
    """Derive an UNCALIBRATED policy from the gateway's known targets.

    A bootstrap policy is never production-ready; it exists so a directly
    constructed gateway still enforces the target/secret rules without a
    deployment calibration step.
    """

    return _policy_from_targets(
        targets,
        state="bootstrap",
        version=MODEL_INPUT_POLICY_VERSION,
        secret_values=secret_values,
    )


def calibrated_model_input_policy(
    targets: Iterable[ModelTarget],
    *,
    secret_values: Iterable[str] = (),
    version: str = MODEL_INPUT_POLICY_CALIBRATED_VERSION,
) -> ModelInputPolicy:
    """Build an explicitly approved deployment policy from an exact pair list.

    This is for a deployment that INJECTS its approved (provider, model) pairs;
    it must never be auto-derived from the set of configured profile targets,
    because dispatch configuration is not a security approval (see R2).
    """

    return _policy_from_targets(
        targets,
        state="calibrated",
        version=version,
        secret_values=secret_values,
    )


def _policy_from_targets(
    targets: Iterable[ModelTarget],
    *,
    state: PolicyLifecycle,
    version: str,
    secret_values: Iterable[str],
) -> ModelInputPolicy:
    items = tuple(targets)
    return ModelInputPolicy(
        version=version,
        state=state,
        approved_destinations=frozenset(_destination(item) for item in items),
        secret_values=tuple(secret_values),
    )


def _destination(target: ModelTarget) -> ApprovedDestination:
    """Project a dispatch target onto its exact approval identity."""

    return ApprovedDestination(provider=target.provider, model=target.model)


def _decision(
    policy: ModelInputPolicy,
    target: ModelTarget,
    *,
    outcome: Literal["allow", "deny"],
    reason: str | None,
    matched_categories: tuple[str, ...],
    content_sha256: str,
) -> ModelInputDecision:
    return ModelInputDecision(
        outcome=outcome,
        reason=reason,
        policy_version=policy.version,
        policy_checksum=policy.checksum,
        policy_state=policy.state,
        target_provider=target.provider,
        target_model=target.model,
        matched_categories=matched_categories,
        content_sha256=content_sha256,
    )


def _content_sha256(request: ModelRequest) -> str:
    payload = request.model_dump(mode="json", include={"messages", "tool_schema"})
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _policy_checksum(policy: ModelInputPolicy) -> str:
    payload = json.dumps(
        {
            "version": policy.version,
            "state": policy.state,
            "approved_destinations": [
                [destination.provider, destination.model]
                for destination in sorted(
                    policy.approved_destinations,
                    key=lambda destination: (destination.provider, destination.model),
                )
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
