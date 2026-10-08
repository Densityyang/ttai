"""Definition / Publication HTTP router (10 product endpoints).

Route bodies stay THIN: request validation, server-side authenticated identity,
service call, response projection.  All lifecycle policy lives in
CustomDefinitionService / PublicationService.

The client may never supply owner identity, authority, scope, lifecycle axes,
publication state, certification or revision pointers: the strict request
contracts below reject every such field with 422 from Pydantic.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field, model_validator

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.build_run import require_build_run
from src.nl2sql.artifacts.custom_definition_execution_service import (
    CalculationInputResolverUnavailable,
    CustomDefinitionExecutionRefused,
    CustomDefinitionExecutionService,
    DefinitionExecutionRefused,
)
from src.nl2sql.artifacts.definition_revalidation import (
    DefinitionRevalidationResult,
)
from src.nl2sql.artifacts.definition_run_record import (
    DefinitionRunPlan,
    DefinitionRunReceipt,
    DefinitionRunRecord,
    DefinitionRunValidation,
)
from src.nl2sql.artifacts.definition_semantics import (
    DefinitionSemantics,
    SemanticAxis,
)
from src.nl2sql.artifacts.publication_service import (
    PublicationConflict,
    PublicationNotEligible,
    PublicationService,
)
from src.nl2sql.artifacts.service import CustomDefinitionService, DefinitionNotFound
from src.nl2sql.contracts import StrictContract, TimeRange
from src.nl2sql.orchestration.custom_calculation_execution import (
    CalculationInputProvenance,
    CustomCalculationExecutionError,
)
from src.nl2sql.orchestration.governed_calculation_inputs import (
    DefinitionExecutionContext,
)
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationSpec,
    ParameterBinding,
)


class CreateDefinitionRequest(StrictContract):
    """A private Definition create.  ParameterContract is DERIVED server-side."""

    title: str = Field(min_length=1, max_length=256)
    calculation: CalculationSpec


class UpdateDraftRequest(StrictContract):
    """A DRAFT edit.  There is deliberately NO client-editable contract field.

    semantics is the caller's DECLARATION of the definition's business meaning
    (A6).  It is deliberately the SHARED DefinitionSemantics model rather than a
    second wire-only copy, so the HTTP surface and the version boundary can
    never disagree about the axis vocabulary.  A declaration carries NO
    authority: DefinitionSemantics forbids every extra field, so an authority /
    lifecycle / canonical / axes / confirmation claim inside it is a 422 before
    any service call can run.

    None (the default, and the shape every pre-existing client sends) means "do
    not touch the declaration", which keeps those requests byte-identical to
    their pre-A6 behaviour.
    """

    title: str | None = Field(default=None, min_length=1, max_length=256)
    calculation: CalculationSpec | None = None
    semantics: DefinitionSemantics | None = None


class ExecuteDefinitionRequest(StrictContract):
    """Concrete per-run parameter VALUES for ONE exact version.

    The version identity comes from the PATH, never from the body, so a caller
    cannot redirect a run at a different definition or version.  Only the
    nested per-run VALUES are accepted here.
    """

    binding: CalculationExecutionBinding
    execution_context: DefinitionExecutionContext = Field(
        default_factory=DefinitionExecutionContext
    )
    # Accept the compact top-level spelling as well; both spellings normalize to
    # the same typed context and no other authority fields are admitted.
    date_mode: Literal["latest_authoritative", "exact_date"] | None = None
    exact_date: date | None = None

    @model_validator(mode="after")
    def normalize_execution_context(self) -> "ExecuteDefinitionRequest":
        if self.date_mode is None and self.exact_date is None:
            return self
        context = DefinitionExecutionContext(
            date_mode=self.date_mode or "latest_authoritative",
            exact_date=self.exact_date,
        )
        if (
            self.execution_context != DefinitionExecutionContext()
            and self.execution_context != context
        ):
            raise ValueError("execution context has conflicting values")
        object.__setattr__(self, "execution_context", context)
        return self


class DefinitionVersionView(StrictContract):
    version: int
    title: str
    calculation: CalculationSpec
    calculation_checksum: str
    parameter_contract_checksum: str
    semantic_closed: bool
    checksum: str
    confirmation: str
    retention: str
    derived_from_definition_id: str | None = None
    derived_from_version: int | None = None
    # Catalogue state for THIS EXACT version only; never inferred from the
    # current Definition axes.
    publication: str = "UNPUBLISHED"
    certification: str = "UNCERTIFIED"
    withdrawn: bool = False
    # A6: the definition-level semantics THIS EXACT version DECLARES, read-only
    # and display-only.  "semantics" is the declaration itself; "declared_axes"
    # is the axis vocabulary it actually populates, in the frozen AXIS_ORDER, so
    # a caller can read what this version declares without re-deriving it.
    # The name is deliberately NOT "semantic_axes": the PATCH response uses that
    # name for the DIFF an edit caused, and one name for two different questions
    # is exactly the kind of overload a client gets wrong.
    # Both are EMPTY when the version declares nothing, and neither carries any
    # authority: they describe business meaning, never a permission, lifecycle
    # or canonicality claim.
    semantics: DefinitionSemantics | None = None
    declared_axes: tuple[SemanticAxis, ...] = ()


class DefinitionView(StrictContract):
    definition_id: str
    owned_by_current_user: bool = True
    current_version: int
    title: str
    calculation: CalculationSpec
    parameter_contract_parameters: tuple[str, ...]
    semantic_closed: bool
    checksum: str
    confirmation: str
    retention: str
    publication: str
    certification: str
    # The OBJECT-LEVEL governance/authority axes (A4).  They are deliberately
    # NOT on DefinitionVersionView: a version's lifecycle does not own them, and
    # duplicating them there would misattribute object state to a version.
    governance: str
    authority: str
    derived_from_definition_id: str | None = None
    derived_from_version: int | None = None


class DraftUpdateResponse(DefinitionView):
    """The DRAFT-edit wire shape, extended with the A6 semantic-axis OUTCOME.

    A SUBCLASS is used deliberately, exactly like ExecutedDefinitionResponse:
    DefinitionView.model_fields stays byte-for-byte frozen (the backend freeze
    contract compares it), while the PATCH wire gains the A6 decision input.
    Existing clients that do not know these fields simply ignore them.

    semantic_axes is the A6 diff THIS edit caused, in the frozen AXIS_ORDER.  A
    NON-EMPTY tuple always comes with version_created and
    requires_business_decision True, so a caller can never miss that the change
    is substantive and that a NEW business decision is required before the new
    draft can be confirmed.  No approval action is performed here: this route
    only SURFACES the requirement.
    """

    semantic_axes: tuple[SemanticAxis, ...] = ()
    version_created: bool = False
    requires_business_decision: bool = False


class DefinitionListResponse(StrictContract):
    definitions: tuple[DefinitionView, ...]


class ExecuteDefinitionResponse(StrictContract):
    """Executed reusable definition result from server-resolved inputs."""

    status: Literal["executed"] = "executed"
    definition_id: str
    version: int
    definition_checksum: str
    calculation_id: str
    spec_checksum: str
    binding_checksum: str
    value: Decimal
    unit: str
    input_provenance: tuple[CalculationInputProvenance, ...]
    data_as_of: datetime | None = None
    time_range: TimeRange | None = None
    calculation_scope: Literal["reusable_custom_definition"]


class ExecutedDefinitionResponse(ExecuteDefinitionResponse):
    """The EXECUTE wire shape, extended with the rerun's degradation flags.

    A SUBCLASS is used deliberately: ``ExecuteDefinitionResponse.model_fields``
    stays byte-for-byte frozen (the backend freeze contract compares it with
    ``==``), while the wire gains ONE additive field so the caller can see that
    this deployment skipped an unconfigured authority check.  Clients that do not
    know the field simply ignore it.
    """

    degradations: tuple[str, ...] = ()


class CalculationExecutionBindingView(StrictContract):
    """The per-run parameter binding AS STORED, plus its CONTENT checksum.

    The domain model exposes its checksum as a COMPUTED PROPERTY, so a plain
    model_dump() silently DROPS it; that hash is exactly what makes two reruns
    of the same version distinguishable in the audit trail, so it is
    materialised here explicitly instead of being lost on the wire.  Nothing is
    re-derived: every field below is copied verbatim from the stored record.
    """

    schema_version: Literal["1.1"] = "1.1"
    calculation_id: str
    spec_checksum: str
    parameters: tuple[ParameterBinding, ...] = ()
    checksum: str


class DefinitionExecutionBindingView(StrictContract):
    """The exact-version binding of ONE rerun, checksum included."""

    definition_id: str
    version: int
    definition_checksum: str
    binding: CalculationExecutionBindingView


class DefinitionRunRecordView(StrictContract):
    """ONE per-run audit record as exposed on the wire.

    It mirrors the immutable DefinitionRunRecord field-for-field WITHOUT
    re-deriving anything: the caller sees the exact binding, plan, validation
    (BOTH revalidation phases, before_resolution and resolved_inputs) and
    receipt that THIS rerun actually produced, plus the explicit degradation
    codes of a deployment that SKIPPED an unconfigured CURRENT-authority check.
    A degraded run is therefore auditable over HTTP and never only inside the
    process.
    """

    run_id: str
    definition_id: str
    version: int
    definition_checksum: str
    binding: DefinitionExecutionBindingView
    plan: DefinitionRunPlan
    validation: DefinitionRunValidation
    degradations: tuple[str, ...] = ()
    receipt: DefinitionRunReceipt | None = None
    created_at: datetime


class DefinitionRunRecordListResponse(StrictContract):
    """Every per-run record of ONE exact version, in insertion order."""

    definition_id: str
    version: int
    runs: tuple[DefinitionRunRecordView, ...]


class ClarificationRequiredResponse(StrictContract):
    """A typed business outcome: the rerun needs a minimal clarification."""

    status: Literal["clarification_required"] = "clarification_required"
    definition_id: str
    version: int
    definition_checksum: str
    reasons: tuple[str, ...]
    unresolved_slots: tuple[str, ...] = ()


class DecisionRequiredResponse(StrictContract):
    """A typed business outcome: a named business-risk decision is required."""

    status: Literal["decision_required"] = "decision_required"
    definition_id: str
    version: int
    definition_checksum: str
    reasons: tuple[str, ...]
    required_decision: str


class ResultUnavailableResponse(StrictContract):
    """A typed business outcome: required CURRENT evidence is unavailable."""

    status: Literal["result_unavailable"] = "result_unavailable"
    definition_id: str
    version: int
    definition_checksum: str
    reasons: tuple[str, ...]
    retryable: bool = False


# The five revalidation branches map onto FOUR response shapes plus the hard 403
# DENY below.  The discriminator keeps them distinguishable on the wire instead
# of collapsing every refusal into one generic 409/503 error code.
ExecuteDefinitionOutcomeResponse = Annotated[
    ExecutedDefinitionResponse
    | ClarificationRequiredResponse
    | DecisionRequiredResponse
    | ResultUnavailableResponse,
    Field(discriminator="status"),
]


def _revalidation_refusal_response(
    refusal: CustomDefinitionExecutionRefused,
) -> (
    ClarificationRequiredResponse
    | DecisionRequiredResponse
    | ResultUnavailableResponse
):
    """Project one typed revalidation refusal onto its OWN wire outcome.

    DENY is a hard 403 policy denial: human confirmation can never repair a
    missing authorization.  CLARIFICATION / BUSINESS-RISK DECISION / UNAVAILABLE
    stay business outcomes and are never collapsed into a generic 409/503.
    """

    result: DefinitionRevalidationResult = refusal.revalidation
    if result.branch == "DENY":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "definition_revalidation_denied",
                "reasons": list(result.reasons),
            },
        )
    if result.branch == "CLARIFICATION":
        return ClarificationRequiredResponse(
            definition_id=refusal.definition_id,
            version=refusal.version,
            definition_checksum=refusal.definition_checksum,
            reasons=result.reasons,
            unresolved_slots=result.unresolved_slots,
        )
    if result.branch == "BUSINESS_RISK_DECISION":
        return DecisionRequiredResponse(
            definition_id=refusal.definition_id,
            version=refusal.version,
            definition_checksum=refusal.definition_checksum,
            reasons=result.reasons,
            required_decision=result.required_decision or "business_risk_decision",
        )
    return ResultUnavailableResponse(
        definition_id=refusal.definition_id,
        version=refusal.version,
        definition_checksum=refusal.definition_checksum,
        reasons=result.reasons,
    )


def owner_identity(auth_user: AuthUser) -> str:
    """The authenticated owner as a STRING.

    ``AuthUser.user_id`` is ``int | str`` because a Backend token may carry a
    numeric subject.  Every personal store here is keyed by a string identity, so
    the conversion happens ONCE at this boundary rather than being guessed deeper.
    """

    return str(auth_user.user_id)


def definition_service(request: Request) -> CustomDefinitionService:
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "custom_definition_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    service: CustomDefinitionService = container.custom_definition_service()
    return service


def definition_execution_service(request: Request) -> CustomDefinitionExecutionService:
    container = getattr(request.app.state, "container", None)
    accessor = getattr(container, "custom_definition_execution_service", None)
    if not callable(accessor):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="calculation_input_resolver_unavailable",
        )
    return cast(CustomDefinitionExecutionService, accessor())


async def publication_service(request: Request) -> PublicationService:
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "publication_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    return await container.publication_service()


def _not_found() -> HTTPException:
    """ONE stable response for foreign AND absent (no existence oracle)."""

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="definition_not_found"
    )


def _run_not_found() -> HTTPException:
    """ONE stable response for a foreign, absent OR unknown run id."""

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="definition_run_not_found"
    )


def _conflict(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)


def _definition_view_fields(definition: Any) -> dict[str, Any]:
    """The ONE projection of a Definition onto its object-level wire fields.

    Returned as a mapping so the DRAFT-edit response can ADD the A6 outcome to
    the EXACT same fields instead of keeping a second, drifting copy of them.
    """

    axes = definition.axes
    current = definition.current_version
    return {
        "definition_id": definition.definition_id,
        "current_version": current.version,
        "title": current.title,
        "calculation": current.calculation,
        "parameter_contract_parameters": tuple(
            parameter.name for parameter in current.parameter_contract.parameters
        ),
        "semantic_closed": current.semantic_closed,
        "checksum": current.checksum,
        "confirmation": axes.confirmation,
        "retention": axes.retention,
        "publication": axes.publication,
        "certification": axes.certification,
        "governance": axes.governance,
        "authority": axes.authority,
        "derived_from_definition_id": current.derived_from_definition_id,
        "derived_from_version": current.derived_from_version,
    }


def _definition_view(definition: Any) -> DefinitionView:
    return DefinitionView(**_definition_view_fields(definition))


def _run_record_view(record: DefinitionRunRecord) -> DefinitionRunRecordView:
    """Project ONE stored record onto the wire WITHOUT losing any audit field."""

    return DefinitionRunRecordView(
        run_id=record.run_id,
        definition_id=record.definition_id,
        version=record.version,
        definition_checksum=record.definition_checksum,
        binding=DefinitionExecutionBindingView(
            definition_id=record.binding.definition_id,
            version=record.binding.version,
            definition_checksum=record.binding.definition_checksum,
            binding=CalculationExecutionBindingView(
                schema_version=record.binding.binding.schema_version,
                calculation_id=record.binding.binding.calculation_id,
                spec_checksum=record.binding.binding.spec_checksum,
                parameters=record.binding.binding.parameters,
                # The domain checksum is a computed property: materialise it.
                checksum=record.binding.binding.checksum,
            ),
        ),
        plan=record.plan,
        validation=record.validation,
        degradations=record.degradations,
        receipt=record.receipt,
        created_at=record.created_at,
    )


async def _exact_catalogue_state(
    request: Request, *, identity_id: str, version: int
) -> tuple[str, str, bool]:
    """Publication state of THIS EXACT version, never the current axes."""

    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "publication_catalogue"):
        return ("UNPUBLISHED", "UNCERTIFIED", False)
    catalogue = await container.publication_catalogue()
    if await catalogue.get(identity_id, version) is None:
        return ("UNPUBLISHED", "UNCERTIFIED", False)
    certification = await catalogue.certification_state(identity_id, version)
    return (
        "PUBLISHED",
        "CERTIFIED" if certification == "certified" else "UNCERTIFIED",
        bool(await catalogue.is_withdrawn(identity_id, version)),
    )


def register_definition_routes(app: Any) -> None:
    """Register the 10 Definition/Publication product endpoints."""

    router = APIRouter(prefix="/api/v2/nl2sql", tags=["nl2sql-v2-definitions"])

    @router.post("/definitions", response_model=DefinitionView)
    async def create_definition(
        request: Request,
        body: CreateDefinitionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        draft = await service.create_draft(
            owner_user_id=owner_identity(auth_user),
            title=body.title,
            calculation=body.calculation,
        )
        definition = await service.get_owned_definition(
            owner_user_id=owner_identity(auth_user), definition_id=draft.definition_id
        )
        return _definition_view(definition)

    @router.get("/definitions", response_model=DefinitionListResponse)
    async def list_definitions(
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionListResponse:
        service = definition_service(request)
        owned = await service.list_owned(owner_user_id=owner_identity(auth_user))
        return DefinitionListResponse(
            definitions=tuple(_definition_view(item) for item in owned)
        )

    @router.get(
        "/definitions/{definition_id}/versions/{version}",
        response_model=DefinitionVersionView,
    )
    async def get_definition_version(
        definition_id: str,
        version: int,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionVersionView:
        service = definition_service(request)
        try:
            exact = await service.get_exact_version(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
            lifecycle = await service.get_version_lifecycle(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        publication, certification, withdrawn = await _exact_catalogue_state(
            request, identity_id=definition_id, version=version
        )
        return DefinitionVersionView(
            version=exact.version,
            title=exact.title,
            calculation=exact.calculation,
            calculation_checksum=exact.calculation.checksum,
            parameter_contract_checksum=exact.parameter_contract.checksum,
            semantic_closed=exact.semantic_closed,
            checksum=exact.checksum,
            confirmation=lifecycle.confirmation,
            retention=lifecycle.retention,
            derived_from_definition_id=exact.derived_from_definition_id,
            derived_from_version=exact.derived_from_version,
            publication=publication,
            certification=certification,
            withdrawn=withdrawn,
            # A6: the declaration of THIS EXACT version, read-only.  A version
            # that declares nothing yields None and an empty axis tuple - it is
            # never an error.
            semantics=exact.semantics,
            declared_axes=exact.semantics.axes() if exact.semantics else (),
        )

    @router.patch(
        "/definitions/{definition_id}/draft",
        response_model=DraftUpdateResponse,
    )
    async def update_definition_draft(
        definition_id: str,
        request: Request,
        body: UpdateDraftRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DraftUpdateResponse:
        """Apply a DRAFT edit and report the A6 axes it changed.

        The response is a SUPERSET of the pre-A6 DefinitionView shape, so an
        existing client keeps working unchanged, while a caller that needs the
        business decision can read semantic_axes / version_created /
        requires_business_decision.  A substantive change opens a NEW draft
        version and requires a NEW business decision; a title-only edit stays on
        the same version and PRESERVES closure.  Nothing is approved here.
        """

        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            outcome = await service.update_draft_with_semantics(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                calculation=body.calculation,
                title=body.title,
                semantics=body.semantics,
            )
            definition = await service.get_owned_definition(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("definition_not_mutable") from exc
        return DraftUpdateResponse(
            **_definition_view_fields(definition),
            semantic_axes=outcome.semantic_axes,
            version_created=outcome.version_created,
            requires_business_decision=outcome.requires_business_decision,
        )

    @router.post(
        "/definitions/{definition_id}/semantic-close",
        response_model=DefinitionView,
    )
    async def semantic_close(
        definition_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            await service.mark_semantic_closed(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
            definition = await service.get_owned_definition(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("semantic_closure_rejected") from exc
        return _definition_view(definition)

    @router.post("/definitions/{definition_id}/confirm", response_model=DefinitionView)
    async def confirm_definition(
        definition_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            definition = await service.confirm(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("definition_lifecycle_invalid") from exc
        return _definition_view(definition)

    @router.post("/definitions/{definition_id}/save", response_model=DefinitionView)
    async def save_definition(
        definition_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            definition = await service.save(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("definition_lifecycle_invalid") from exc
        return _definition_view(definition)

    @router.post(
        "/definitions/{definition_id}/revisions",
        response_model=DefinitionView,
    )
    async def create_definition_revision(
        definition_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            await service.create_revision(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
            definition = await service.get_owned_definition(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("revision_lifecycle_invalid") from exc
        return _definition_view(definition)

    @router.post(
        "/definitions/{definition_id}/versions/{version}/publish",
        response_model=DefinitionVersionView,
    )
    async def publish_definition_version(
        definition_id: str,
        version: int,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionVersionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        publications = await publication_service(request)
        try:
            await publications.publish(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
            exact = await service.get_exact_version(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
            lifecycle = await service.get_version_lifecycle(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except PublicationConflict as exc:
            raise _conflict(exc.reason) from exc
        except PublicationNotEligible as exc:
            raise _conflict(exc.reason) from exc
        publication, certification, withdrawn = await _exact_catalogue_state(
            request, identity_id=definition_id, version=version
        )
        return DefinitionVersionView(
            version=exact.version,
            title=exact.title,
            calculation=exact.calculation,
            calculation_checksum=exact.calculation.checksum,
            parameter_contract_checksum=exact.parameter_contract.checksum,
            semantic_closed=exact.semantic_closed,
            checksum=exact.checksum,
            confirmation=lifecycle.confirmation,
            retention=lifecycle.retention,
            derived_from_definition_id=exact.derived_from_definition_id,
            derived_from_version=exact.derived_from_version,
            publication=publication,
            certification=certification,
            withdrawn=withdrawn,
            # A6: the declaration of THIS EXACT version, read-only.  A version
            # that declares nothing yields None and an empty axis tuple - it is
            # never an error.
            semantics=exact.semantics,
            declared_axes=exact.semantics.axes() if exact.semantics else (),
        )

    @router.post(
        "/definitions/{definition_id}/versions/{version}/execute",
        response_model=ExecuteDefinitionOutcomeResponse,
    )
    async def execute_definition_version(
        definition_id: str,
        version: int,
        request: Request,
        body: ExecuteDefinitionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ExecuteDefinitionOutcomeResponse:
        try:
            outcome = await definition_execution_service(request).execute(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
                binding=body.binding,
                execution_context=body.execution_context,
            )
        except DefinitionExecutionRefused as exc:
            return _revalidation_refusal_response(exc.refusal)
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except CalculationInputResolverUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=exc.code,
            ) from exc
        except CustomCalculationExecutionError as exc:
            raise _conflict(exc.code) from exc
        except ValueError as exc:
            raise _conflict("execution_binding_invalid") from exc
        result = outcome.result
        return ExecutedDefinitionResponse(
            definition_id=outcome.definition_id,
            version=outcome.version,
            definition_checksum=outcome.definition_checksum,
            calculation_id=result.calculation_id,
            spec_checksum=result.spec_checksum,
            binding_checksum=result.binding_checksum,
            value=result.value,
            unit=result.unit,
            input_provenance=result.input_provenance,
            data_as_of=result.data_as_of,
            time_range=result.time_range,
            calculation_scope=result.calculation_scope,
            degradations=outcome.degradations,
        )

    @router.get(
        "/definitions/{definition_id}/versions/{version}/runs",
        response_model=DefinitionRunRecordListResponse,
    )
    async def list_definition_version_runs(
        definition_id: str,
        version: int,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionRunRecordListResponse:
        """Every per-run audit record of THIS exact version, owner-scoped.

        The owner check runs FIRST through the SAME resolution as every other
        definition reader, so a foreign identity is indistinguishable from an
        absent definition (404, never 403) and cannot use this route as an
        existence oracle.
        """

        service = definition_service(request)
        try:
            await service.get_exact_version(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        records = await definition_execution_service(
            request
        ).run_records.list_for_version(definition_id=definition_id, version=version)
        return DefinitionRunRecordListResponse(
            definition_id=definition_id,
            version=version,
            runs=tuple(_run_record_view(record) for record in records),
        )

    @router.get(
        "/definitions/{definition_id}/versions/{version}/runs/{run_id}",
        response_model=DefinitionRunRecordView,
    )
    async def get_definition_version_run(
        definition_id: str,
        version: int,
        run_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionRunRecordView:
        """ONE per-run audit record, owner-scoped by the SAME first check."""

        service = definition_service(request)
        try:
            await service.get_exact_version(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        record = await definition_execution_service(request).run_records.get(
            definition_id=definition_id, version=version, run_id=run_id
        )
        if record is None:
            raise _run_not_found()
        return _run_record_view(record)

    app.include_router(router)


__all__ = [
    "ClarificationRequiredResponse",
    "CreateDefinitionRequest",
    "DecisionRequiredResponse",
    "DefinitionListResponse",
    "DefinitionRunRecordListResponse",
    "DefinitionRunRecordView",
    "DefinitionVersionView",
    "DefinitionView",
    "DraftUpdateResponse",
    "ExecuteDefinitionOutcomeResponse",
    "ExecuteDefinitionRequest",
    "ExecuteDefinitionResponse",
    "ExecutedDefinitionResponse",
    "ResultUnavailableResponse",
    "UpdateDraftRequest",
    "register_definition_routes",
]
