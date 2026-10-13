"""Fail-closed future broker interface; no live authority or DB adapter is wired."""

from __future__ import annotations

from typing import Protocol

from src.nl2sql.orchestration.data_request import (
    DataRequest,
    DataRequestBudget,
    DataRequestDenied,
    EffectiveDataScope,
    FrozenDataset,
    GovernedSQLCandidate,
    TrustedExecutionResult,
    VerifiedDataAuthorization,
    build_frozen_dataset,
    derive_effective_scope,
)


class DataBrokerUnavailable(RuntimeError):
    """Safe refusal when no trusted authority/compiler/executor is wired."""

    def __init__(self) -> None:
        super().__init__("governed_data_broker_unavailable")


class DataAuthorizationAuthority(Protocol):
    """Future server-side source of owner-bound relation and column grants."""

    async def verify_request(
        self,
        request: DataRequest,
    ) -> VerifiedDataAuthorization | None: ...


class DeterministicDataCompiler(Protocol):
    """Future compiler; it receives typed scope and never model-authored SQL."""

    def compile_request(
        self,
        request: DataRequest,
        authorization: VerifiedDataAuthorization,
        effective_scope: EffectiveDataScope,
    ) -> GovernedSQLCandidate: ...


class GovernedDataExecutor(Protocol):
    """Future trusted adapter; no SQLAlchemy session crosses this contract."""

    async def execute_candidate(
        self,
        candidate: GovernedSQLCandidate,
        budget: DataRequestBudget,
    ) -> TrustedExecutionResult: ...


class GovernedDataBroker:
    """No-default-allow orchestration seam for a future trusted broker stack."""

    def __init__(
        self,
        *,
        authority: DataAuthorizationAuthority | None = None,
        compiler: DeterministicDataCompiler | None = None,
        executor: GovernedDataExecutor | None = None,
    ) -> None:
        self._authority = authority
        self._compiler = compiler
        self._executor = executor

    async def request_dataset(self, request: DataRequest) -> FrozenDataset:
        """Require all three trusted components before issuing frozen data."""

        if self._authority is None or self._compiler is None or self._executor is None:
            raise DataBrokerUnavailable()

        try:
            authorization = await self._authority.verify_request(request)
            if authorization is None:
                raise DataRequestDenied()
            effective_scope = derive_effective_scope(request, authorization)
            candidate = self._compiler.compile_request(
                request,
                authorization,
                effective_scope,
            )
            execution = await self._executor.execute_candidate(
                candidate,
                request.budget,
            )
            return build_frozen_dataset(
                request=request,
                authorization=authorization,
                candidate=candidate,
                execution=execution,
            )
        except (DataBrokerUnavailable, DataRequestDenied):
            raise
        except Exception:
            # Adapter exceptions may contain SQL, private scopes, or credentials.
            raise DataBrokerUnavailable() from None
