"""Fail-closed classification for the future governed P5 runtime."""

from dataclasses import dataclass
from enum import StrEnum

from src.nl2sql.config.settings import AgentConfig, get_agent_config


class P5ExecutionMode(StrEnum):
    DISABLED = "disabled"
    TRUSTED_TEMPLATE = "trusted_template"
    INFRA_DEV_UNSAFE = "infra_dev_unsafe"


class P5EvidenceStatus(StrEnum):
    NOT_VERIFIED = "not_verified"


class P5ReadinessReason(StrEnum):
    DYNAMIC_CALCULATION_DISABLED = "dynamic_calculation_disabled"
    CODEACT_MODE_DISABLED = "codeact_mode_disabled"
    EXISTING_EXECUTION_POLICY_DENIED = "existing_execution_policy_denied"
    TRUSTED_TEMPLATE_POLICY_ALLOWED = "trusted_template_policy_allowed"
    UNSAFE_DEV_RESTRICTED_TO_INFRA_DEV = "unsafe_dev_restricted_to_infra_dev"
    UNSAFE_DEV_REQUIRES_INFRA_DEV = "unsafe_dev_requires_infra_dev"
    OS_ISOLATION_NOT_VERIFIED = "os_isolation_not_verified"
    GOVERNED_BROKER_NOT_VERIFIED = "governed_broker_not_verified"
    PRODUCTION_ARBITRARY_CODE_NOT_READY = "production_arbitrary_code_not_ready"


@dataclass(frozen=True)
class P5RuntimeReadiness:
    """Policy classification without accepting caller-supplied attestations.

    Isolation and broker evidence deliberately have no configurable success
    state in this slice. A future verified evidence source must be added
    separately before production arbitrary-code readiness can change.
    """

    execution_mode: P5ExecutionMode
    reason_codes: tuple[P5ReadinessReason, ...]
    os_isolation_evidence: P5EvidenceStatus = P5EvidenceStatus.NOT_VERIFIED
    governed_broker_evidence: P5EvidenceStatus = P5EvidenceStatus.NOT_VERIFIED

    @property
    def trusted_template_permitted(self) -> bool:
        return self.execution_mode is P5ExecutionMode.TRUSTED_TEMPLATE

    @property
    def unsafe_dev_permitted(self) -> bool:
        return self.execution_mode is P5ExecutionMode.INFRA_DEV_UNSAFE

    @property
    def production_arbitrary_code_ready(self) -> bool:
        return False


def assess_p5_runtime_readiness(
    config: AgentConfig | None = None,
) -> P5RuntimeReadiness:
    """Classify existing local policy; never attest isolation or broker readiness."""

    effective_config = config if config is not None else get_agent_config()
    reasons: list[P5ReadinessReason] = []

    if not effective_config.enable_dynamic_calc:
        mode = P5ExecutionMode.DISABLED
        reasons.append(P5ReadinessReason.DYNAMIC_CALCULATION_DISABLED)
    elif effective_config.codeact_mode == "disabled":
        mode = P5ExecutionMode.DISABLED
        reasons.append(P5ReadinessReason.CODEACT_MODE_DISABLED)
    elif not effective_config.codeact_capability()[0]:
        mode = P5ExecutionMode.DISABLED
        reasons.append(P5ReadinessReason.EXISTING_EXECUTION_POLICY_DENIED)
    elif effective_config.codeact_mode == "trusted-template":
        mode = P5ExecutionMode.TRUSTED_TEMPLATE
        reasons.append(P5ReadinessReason.TRUSTED_TEMPLATE_POLICY_ALLOWED)
    elif (
        effective_config.codeact_mode == "unsafe-dev"
        and effective_config.service_mode == "infra-dev"
    ):
        mode = P5ExecutionMode.INFRA_DEV_UNSAFE
        reasons.append(P5ReadinessReason.UNSAFE_DEV_RESTRICTED_TO_INFRA_DEV)
    else:
        # Keep unknown or invalid policy combinations fail-closed.
        mode = P5ExecutionMode.DISABLED
        reasons.append(P5ReadinessReason.UNSAFE_DEV_REQUIRES_INFRA_DEV)

    reasons.extend(
        (
            P5ReadinessReason.OS_ISOLATION_NOT_VERIFIED,
            P5ReadinessReason.GOVERNED_BROKER_NOT_VERIFIED,
            P5ReadinessReason.PRODUCTION_ARBITRARY_CODE_NOT_READY,
        )
    )
    return P5RuntimeReadiness(execution_mode=mode, reason_codes=tuple(reasons))
