"""Current-state revalidation for a SAVED Custom Definition rerun (A7, 5.2.2).

A SAVED definition rerun is never authorised by the mere fact that the definition
was saved once.  Every rerun re-proves the CURRENT authorization, active release,
data snapshot, governed-metric authority, freshness/DQ and remaining budget, and
resolves to exactly one typed branch::

    EXECUTE / CLARIFICATION / BUSINESS_RISK_DECISION / DENY / UNAVAILABLE

The gate is FAIL-CLOSED by default: an absent provider or absent evidence never
silently passes.  A missing provider is UNAVAILABLE, never EXECUTE.  Human
confirmation can never repair a missing authorization, so DENY is never
converted into a pending confirmation.

``strict=True`` (the default, and the ONLY behaviour in product mode) treats an
UNCONFIGURED authority provider as UNAVAILABLE.  ``strict=False`` is the explicit
infra-dev/demo relaxation: an UNCONFIGURED provider is SKIPPED and the skipped
check is recorded in ``DefinitionRevalidationResult.degradations`` so a
deployment that has not wired the external authority is never a silent pass.  A
provider that EXISTS but yields no evidence is UNAVAILABLE in BOTH modes: "not
configured" and "cannot obtain evidence" are different states, and only the
first is skippable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from src.nl2sql.artifacts.custom_definition import (
    DefinitionVersion,
    DefinitionVersionLifecycle,
)
from src.nl2sql.contracts import StrictContract
from src.nl2sql.orchestration.custom_calculation_execution import (
    ResolvedCalculationInput,
)
from src.nl2sql.orchestration.governed_calculation_inputs import (
    DefinitionExecutionContext,
)
from src.nl2sql.semantic.calculation_contract import CalculationExecutionBinding

__all__ = [
    "ActiveReleaseEvidence",
    "ActiveReleaseProvider",
    "AuthorizationEvidence",
    "AuthorizationEvidenceProvider",
    "DataSnapshotEvidence",
    "DataSnapshotProvider",
    "DegradationCode",
    "DefinitionRevalidationGate",
    "DefinitionRevalidationResult",
    "FreshnessDQProvider",
    "GovernedMetricAuthority",
    "InputFreshnessDQ",
    "RemainingBudgetProvider",
    "RevalidationBranch",
    "RevalidationReason",
    "RiskDecisionProvider",
]

RevalidationBranch = Literal[
    "EXECUTE",
    "CLARIFICATION",
    "BUSINESS_RISK_DECISION",
    "DENY",
    "UNAVAILABLE",
]

RevalidationReason = Literal[
    # SAVED lifecycle gate
    "definition_version_not_confirmed",
    "definition_version_not_saved",
    # current authorization
    "authorization_evidence_unavailable",
    "authorization_denied",
    # active release / data snapshot
    "active_release_unavailable",
    "data_snapshot_unavailable",
    # current governed-metric authority
    "governed_metric_authority_unavailable",
    "governed_metric_retired",
    "input_metric_identity_unbound",
    # freshness / DQ
    "freshness_evidence_unavailable",
    "freshness_stale",
    "freshness_unknown",
    "dq_failed",
    "dq_unknown",
    # remaining budget
    "budget_evidence_unavailable",
    "budget_exhausted",
    # independent risk gate
    "business_risk_decision_required",
]

# The EXPLICIT degradation codes for an UNCONFIGURED external authority check.
# Each name says "unconfigured", never "passed": a skipped check is recorded as
# an absence of authority, not as evidence that the check succeeded.
DegradationCode = Literal[
    "authorization_evidence_unconfigured",
    "active_release_unconfigured",
    "data_snapshot_unconfigured",
    "freshness_evidence_unconfigured",
    "budget_evidence_unconfigured",
]

_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"
_ROLE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


class AuthorizationEvidence(StrictContract):
    """The CURRENT authorization state of the requesting owner.

    authorization_revision is a server-owned authority pointer, never a client
    value.  A rerun whose evidence is absent, disabled or non-permitting is
    refused; it is never converted into a pending human confirmation.
    """

    authorization_revision: str = Field(min_length=1, max_length=256)
    agent_enabled: bool = True
    permitted: bool = True


class ActiveReleaseEvidence(StrictContract):
    """The CURRENT active semantic release the rerun would execute against."""

    release_id: str = Field(min_length=1, max_length=256)
    release_checksum: str = Field(pattern=_CHECKSUM_PATTERN)


class DataSnapshotEvidence(StrictContract):
    """The CURRENT schema/data snapshot the rerun would execute against."""

    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_checksum: str = Field(pattern=_CHECKSUM_PATTERN)


class InputFreshnessDQ(StrictContract):
    """The per-input freshness and data-quality judgement of ONE resolved role."""

    role: str = Field(pattern=_ROLE_PATTERN)
    metric_key: str = Field(min_length=1, max_length=256)
    freshness: Literal["fresh", "stale", "unknown"]
    dq: Literal["pass", "fail", "unknown"] = "unknown"
    data_as_of: datetime | None = None


class DefinitionRevalidationResult(StrictContract):
    """One typed revalidation outcome.  Exactly one branch, by construction."""

    branch: RevalidationBranch
    reasons: tuple[RevalidationReason, ...] = Field(default=(), max_length=16)
    # Explicit "this check was skipped because no authority is configured".
    # Deliberately NOT folded into ``reasons``: an EXECUTE branch must not carry
    # a refusal reason, yet its degradations must stay visible.
    degradations: tuple[str, ...] = Field(default=(), max_length=16)
    unresolved_slots: tuple[str, ...] = Field(default=(), max_length=32)
    required_decision: str | None = Field(default=None, max_length=256)
    authorization_revision: str | None = Field(default=None, max_length=256)
    release_id: str | None = Field(default=None, max_length=256)
    snapshot_id: str | None = Field(default=None, max_length=256)
    freshness: tuple[InputFreshnessDQ, ...] = Field(default=(), max_length=32)
    checked_at: datetime

    @model_validator(mode="after")
    def validate_branch_shape(self) -> DefinitionRevalidationResult:
        if self.branch == "EXECUTE":
            if self.reasons:
                raise ValueError("EXECUTE must not carry a refusal reason")
        elif not self.reasons:
            raise ValueError("a non-EXECUTE revalidation must carry a reason")
        if self.branch == "BUSINESS_RISK_DECISION" and self.required_decision is None:
            raise ValueError("a business-risk decision must name the decision")
        return self


class AuthorizationEvidenceProvider(Protocol):
    async def current_authorization(
        self, *, owner_user_id: str
    ) -> AuthorizationEvidence | None: ...


class ActiveReleaseProvider(Protocol):
    async def active_release(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> ActiveReleaseEvidence | None: ...


class DataSnapshotProvider(Protocol):
    async def data_snapshot(
        self, *, owner_user_id: str
    ) -> DataSnapshotEvidence | None: ...


class FreshnessDQProvider(Protocol):
    async def assess(
        self,
        *,
        owner_user_id: str,
        definition: DefinitionVersion,
        resolved_inputs: tuple[ResolvedCalculationInput, ...],
        execution_context: DefinitionExecutionContext,
    ) -> tuple[InputFreshnessDQ, ...]: ...


class RemainingBudgetProvider(Protocol):
    async def remaining_budget(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> int: ...


class RiskDecisionProvider(Protocol):
    async def required_decision(
        self,
        *,
        owner_user_id: str,
        definition: DefinitionVersion,
        binding: CalculationExecutionBinding,
        execution_context: DefinitionExecutionContext,
    ) -> str | None: ...


GovernedMetricAuthority = Callable[[str], bool]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DefinitionRevalidationGate:
    """Re-prove the CURRENT gates for one SAVED rerun, fail-closed by default.

    Every provider is optional at CONSTRUCTION time, but an absent provider is
    never a pass: it resolves to UNAVAILABLE with a named reason.  The only
    authority this class trusts is the evidence a provider actually returns.

    ``strict=True`` is the DEFAULT, so any direct construction fails closed.  Only
    a caller that has explicitly decided to relax a NON-product deployment may
    pass ``strict=False``; then an UNCONFIGURED provider is skipped and named in
    ``degradations``, while a CONFIGURED provider that yields no evidence still
    resolves to UNAVAILABLE.
    """

    def __init__(
        self,
        *,
        authorization_provider: AuthorizationEvidenceProvider | None = None,
        active_release_provider: ActiveReleaseProvider | None = None,
        data_snapshot_provider: DataSnapshotProvider | None = None,
        freshness_dq_provider: FreshnessDQProvider | None = None,
        budget_provider: RemainingBudgetProvider | None = None,
        governed_metric_authority: GovernedMetricAuthority | None = None,
        risk_decision_provider: RiskDecisionProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        strict: bool = True,
    ) -> None:
        self._authorization_provider = authorization_provider
        self._active_release_provider = active_release_provider
        self._data_snapshot_provider = data_snapshot_provider
        self._freshness_dq_provider = freshness_dq_provider
        self._budget_provider = budget_provider
        self._governed_metric_authority = governed_metric_authority
        self._risk_decision_provider = risk_decision_provider
        self._clock = clock or _utcnow
        self._strict = strict

    def _result(
        self,
        branch: RevalidationBranch,
        reason: RevalidationReason | None = None,
        *,
        authorization_revision: str | None = None,
        release_id: str | None = None,
        snapshot_id: str | None = None,
        freshness: tuple[InputFreshnessDQ, ...] = (),
        unresolved_slots: tuple[str, ...] = (),
        required_decision: str | None = None,
        degradations: tuple[str, ...] = (),
    ) -> DefinitionRevalidationResult:
        return DefinitionRevalidationResult(
            branch=branch,
            reasons=() if reason is None else (reason,),
            degradations=degradations,
            unresolved_slots=unresolved_slots,
            required_decision=required_decision,
            authorization_revision=authorization_revision,
            release_id=release_id,
            snapshot_id=snapshot_id,
            freshness=freshness,
            checked_at=self._clock(),
        )

    async def revalidate_before_resolution(
        self,
        *,
        owner_user_id: str,
        definition: DefinitionVersion,
        lifecycle: DefinitionVersionLifecycle,
        binding: CalculationExecutionBinding,
        execution_context: DefinitionExecutionContext,
    ) -> DefinitionRevalidationResult:
        """Every gate that must be proven BEFORE governed inputs are fetched."""

        # 1. The exact version must be a CONFIRMED + SAVED reusable definition.
        #    A DRAFT is not a reusable definition and is never executed here.
        if lifecycle.confirmation != "CONFIRMED":
            return self._result("DENY", "definition_version_not_confirmed")
        if lifecycle.retention != "SAVED":
            return self._result("DENY", "definition_version_not_saved")

        # Skipped checks of THIS revalidation, in evaluation order.  In strict
        # (product) mode this stays empty: an unconfigured authority is
        # UNAVAILABLE, never a silent pass.  Outside product it names exactly
        # which authority the deployment has not wired.
        degradations: list[str] = []

        # 2. CURRENT authorization.  An UNCONFIGURED provider is UNAVAILABLE in
        #    strict mode and a recorded degradation otherwise; a provider that
        #    EXISTS but returns no evidence is UNAVAILABLE in BOTH modes; a
        #    present-but-denying authority is DENY.  Confirmation never repairs
        #    any of them.
        if self._authorization_provider is None:
            if self._strict:
                return self._result("UNAVAILABLE", "authorization_evidence_unavailable")
            degradations.append("authorization_evidence_unconfigured")
            authorization = None
        else:
            authorization = await self._authorization_provider.current_authorization(
                owner_user_id=owner_user_id
            )
            if authorization is None:
                return self._result("UNAVAILABLE", "authorization_evidence_unavailable")
            if not authorization.permitted or not authorization.agent_enabled:
                return self._result("DENY", "authorization_denied")

        # 3. CURRENT active release.
        if self._active_release_provider is None:
            if self._strict:
                return self._result("UNAVAILABLE", "active_release_unavailable")
            degradations.append("active_release_unconfigured")
            release = None
        else:
            release = await self._active_release_provider.active_release(
                owner_user_id=owner_user_id,
                definition_id=definition.definition_id,
                version=definition.version,
            )
            if release is None:
                return self._result("UNAVAILABLE", "active_release_unavailable")

        # 4. CURRENT data snapshot.
        if self._data_snapshot_provider is None:
            if self._strict:
                return self._result("UNAVAILABLE", "data_snapshot_unavailable")
            degradations.append("data_snapshot_unconfigured")
            snapshot = None
        else:
            snapshot = await self._data_snapshot_provider.data_snapshot(
                owner_user_id=owner_user_id
            )
            if snapshot is None:
                return self._result("UNAVAILABLE", "data_snapshot_unavailable")

        # 5. CURRENT governed-metric authority for every declared input.  This
        #    authority is BOUND to the definition service and is therefore always
        #    available; it is NEVER a skippable check.
        authority = self._governed_metric_authority
        if authority is None:
            return self._result("UNAVAILABLE", "governed_metric_authority_unavailable")
        unbound: list[str] = []
        for requirement in definition.calculation.inputs:
            if requirement.provenance != "published_gold":
                continue
            if requirement.metric_key is None:
                unbound.append(requirement.role)
                continue
            if not authority(requirement.metric_key):
                # The input's metric identity is no longer governed: the saved
                # definition may not silently keep computing from a retired key.
                # A skipped UNCONFIGURED authority above can never turn this DENY
                # into an EXECUTE.
                return self._result(
                    "DENY",
                    "governed_metric_retired",
                    degradations=tuple(degradations),
                )
        if unbound:
            return self._result(
                "CLARIFICATION",
                "input_metric_identity_unbound",
                unresolved_slots=tuple(unbound),
                degradations=tuple(degradations),
            )

        # 6. REMAINING budget, checked BEFORE any governed fetch so an exhausted
        #    budget can never reach the fetcher.
        if self._budget_provider is None:
            if self._strict:
                return self._result("UNAVAILABLE", "budget_evidence_unavailable")
            degradations.append("budget_evidence_unconfigured")
            remaining = None
        else:
            remaining = await self._budget_provider.remaining_budget(
                owner_user_id=owner_user_id,
                definition_id=definition.definition_id,
                version=definition.version,
            )
            if remaining is None:
                return self._result("UNAVAILABLE", "budget_evidence_unavailable")
            if remaining <= 0:
                return self._result("UNAVAILABLE", "budget_exhausted")

        # 7. Independent risk gate.  A configured risk authority may require a
        #    named business-risk decision; that is a business outcome, never a
        #    transport error.
        decision = await self._risk_decision(
            owner_user_id=owner_user_id,
            definition=definition,
            binding=binding,
            execution_context=execution_context,
        )
        if decision is not None:
            return self._result(
                "BUSINESS_RISK_DECISION",
                "business_risk_decision_required",
                required_decision=decision,
                degradations=tuple(degradations),
            )

        return self._result(
            "EXECUTE",
            authorization_revision=(
                None
                if authorization is None
                else authorization.authorization_revision
            ),
            release_id=None if release is None else release.release_id,
            snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            degradations=tuple(degradations),
        )

    async def revalidate_resolved_inputs(
        self,
        *,
        owner_user_id: str,
        definition: DefinitionVersion,
        resolved_inputs: tuple[ResolvedCalculationInput, ...],
        execution_context: DefinitionExecutionContext,
    ) -> DefinitionRevalidationResult:
        """Freshness/DQ gate over the ACTUAL resolved evidence of this rerun."""

        provider = self._freshness_dq_provider
        if provider is None:
            if self._strict:
                return self._result("UNAVAILABLE", "freshness_evidence_unavailable")
            # An UNCONFIGURED freshness/DQ authority is skipped and NAMED; this is
            # never a claim that the evidence passed.
            return self._result(
                "EXECUTE",
                degradations=("freshness_evidence_unconfigured",),
            )
        assessed = tuple(
            await provider.assess(
                owner_user_id=owner_user_id,
                definition=definition,
                resolved_inputs=resolved_inputs,
                execution_context=execution_context,
            )
        )
        declared_roles = tuple(item.role for item in definition.calculation.inputs)
        assessed_roles = tuple(item.role for item in assessed)
        if set(assessed_roles) != set(declared_roles) or len(assessed_roles) != len(
            set(assessed_roles)
        ):
            return self._result(
                "UNAVAILABLE", "freshness_evidence_unavailable", freshness=assessed
            )
        if any(item.dq == "fail" for item in assessed):
            return self._result("UNAVAILABLE", "dq_failed", freshness=assessed)
        if any(item.dq == "unknown" for item in assessed):
            return self._result("UNAVAILABLE", "dq_unknown", freshness=assessed)
        if any(item.freshness == "stale" for item in assessed):
            # Stale is recoverable by the caller (refresh / exact date), so it is
            # a clarification rather than a refusal.
            return self._result("CLARIFICATION", "freshness_stale", freshness=assessed)
        if any(item.freshness == "unknown" for item in assessed):
            return self._result("UNAVAILABLE", "freshness_unknown", freshness=assessed)
        return self._result("EXECUTE", freshness=assessed)

    # --- risk adapter (an absent risk authority requires no decision) ---------
    async def _risk_decision(
        self,
        *,
        owner_user_id: str,
        definition: DefinitionVersion,
        binding: CalculationExecutionBinding,
        execution_context: DefinitionExecutionContext,
    ) -> str | None:
        if self._risk_decision_provider is None:
            return None
        return await self._risk_decision_provider.required_decision(
            owner_user_id=owner_user_id,
            definition=definition,
            binding=binding,
            execution_context=execution_context,
        )
