"""Custom Definition service: ALL definition lifecycle policy, one place.

The service owns the version boundary, the closure gate, the axis implication
invariants and the per-version lifecycle derivation.  The version boundary is
driven by the A6 material axis diff (definition_semantics.semantic_axes): a
substantive change opens a new version and demands a new business decision, a
non-substantive one updates in place, and a within-contract parameter rebinding
never creates a version (A5).  Storage is delegated to a swappable
DefinitionStore PORT, so the durable Control-PostgreSQL backend and the
process-local backend run the SAME policy instead of two copies of it.

Owner identity is ALWAYS supplied by the caller from the authenticated request
context.  A cross-user access raises the same not-found as an absent definition.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from src.nl2sql.artifacts.custom_definition import (
    CustomDefinition,
    DefinitionAxes,
    DefinitionExecutionBinding,
    DefinitionVersion,
    DefinitionVersionLifecycle,
    ParameterContract,
    derive_parameter_contract,
    new_definition_id,
    utcnow,
)
from src.nl2sql.artifacts.definition_semantics import (
    DefinitionSemantics,
    SemanticAxis,
    SemanticDeclaration,
    semantic_axes,
)
from src.nl2sql.artifacts.definition_store import (
    DefinitionStore,
    InMemoryDefinitionStore,
)
from src.nl2sql.artifacts.repository import ArtifactNotFound
from src.nl2sql.semantic.calculation_contract import CalculationSpec


def _default_governed_metric_keys() -> frozenset[str]:
    """Fail closed when no applicable server-owned authority is injected."""

    return frozenset()


def _declaration_of(version: DefinitionVersion) -> SemanticDeclaration:
    """The A6 declarative surface of ONE version, ready for a PURE diff.

    Only CHECKSUMS are composed here: the diff never re-derives identity and
    never needs the definition module, so the two can never disagree about what
    "the expression" or "the parameter contract" is.
    """

    return SemanticDeclaration(
        expression_checksum=version.calculation.checksum,
        parameter_contract_checksum=version.parameter_contract.checksum,
        semantics=version.semantics,
    )


@dataclass(frozen=True)
class DraftSemanticUpdate:
    """ONE draft edit OUTCOME: the version plus the A6 material-axis diff.

    semantic_axes is the A6 diff between the declaration BEFORE the edit and the
    declaration AFTER it, in the frozen AXIS_ORDER.  The service does NOT
    implement any approval workflow (a later slice owns that); it only SURFACES
    the axes so the caller can require the business decision A6 mandates.
    """

    version: DefinitionVersion
    semantic_axes: tuple[SemanticAxis, ...]
    # A SUBSTANTIVE edit opens a NEW draft version; an in-place edit (for example
    # a title-only change) does not.
    version_created: bool
    # A substantive edit can never ride on the OLD closure proof: the caller must
    # re-prove closure and re-confirm.
    requires_business_decision: bool

    @property
    def material(self) -> bool:
        return bool(self.semantic_axes)


@dataclass(frozen=True)
class _ResolvedDraftUpdate:
    """The fully resolved NEW state of a draft edit, before it is stored."""

    calculation: CalculationSpec
    parameter_contract: ParameterContract
    title: str
    semantics: DefinitionSemantics | None
    semantic_axes: tuple[SemanticAxis, ...]


class DefinitionNotFound(LookupError):
    def __init__(self) -> None:
        super().__init__("definition_not_found")


class CustomDefinitionService:
    """Definition lifecycle policy over a swappable DefinitionStore PORT.

    A DRAFT edit with NO A6 material axis (for example a title-only edit) happens
    IN PLACE and PRESERVES the closure proof.  An edit with AT LEAST ONE material
    axis opens the NEXT version number and INVALIDATES closure.  Once a version is
    CONFIRMED/SAVED it is immutable, so any further edit goes through the positive
    revision path - an existing version is never mutated in place.
    """

    def __init__(
        self,
        *,
        store: DefinitionStore | None = None,
        governed_metric_key_resolver: Callable[[str], bool] | None = None,
        governed_metric_keys: Collection[str] | None = None,
    ) -> None:
        # Storage only.  The service never keeps a second copy of definition
        # state, so the durable and process-local backends cannot diverge.
        self._store: DefinitionStore = (
            store if store is not None else InMemoryDefinitionStore()
        )
        if governed_metric_key_resolver is not None and governed_metric_keys is not None:
            raise ValueError("provide a metric resolver or a metric set, not both")
        self._governed_metric_key_resolver = governed_metric_key_resolver
        self._governed_metric_keys = frozenset(
            governed_metric_keys
            if governed_metric_keys is not None
            else _default_governed_metric_keys()
        )

    # --- helpers ---------------------------------------------------------
    async def _owned(
        self, *, owner_user_id: str, definition_id: str
    ) -> CustomDefinition:
        definition = await self._store.get_definition(definition_id=definition_id)
        if definition is None or definition.owner_user_id != owner_user_id:
            raise DefinitionNotFound()
        return definition

    # --- bounded PUBLIC owner-scoped API ---------------------------------
    async def get_owned_definition(
        self, *, owner_user_id: str, definition_id: str
    ) -> CustomDefinition:
        """Own public accessor.  Foreign == absent, both raise DefinitionNotFound.

        Cross-service callers (Publication, Product Library) MUST use this or
        another public method rather than the private owner-check helper.
        """

        return await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )

    async def get_version_lifecycle(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> DefinitionVersionLifecycle:
        """The EXACT private lifecycle of one version, never the current axes.

        The owner check runs FIRST, so a foreign caller gets the same failure as
        an absent definition and cannot probe whether a version exists.
        """

        # Reuse the SAME resolution as get_exact_version, so a DRAFT version and
        # a confirmed version are treated consistently by both readers.
        exact = await self.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )
        recorded = await self._store.get_lifecycle(
            definition_id=definition_id, version=version
        )
        if recorded is not None:
            return recorded
        # A version with no explicit lifecycle record is derived from its own
        # immutable state, never from the mutable current axes.
        if exact.semantic_closed and (
            await self._store.get_version(
                definition_id=definition_id, version=version
            )
            is not None
        ):
            return DefinitionVersionLifecycle(
                confirmation="CONFIRMED",
                retention="SAVED",
            )
        return DefinitionVersionLifecycle()

    # --- lifecycle -------------------------------------------------------
    async def create_draft(
        self,
        *,
        owner_user_id: str,
        title: str,
        calculation: CalculationSpec,
        parameter_contract: ParameterContract | None = None,
        semantics: DefinitionSemantics | None = None,
    ) -> DefinitionVersion:
        if not owner_user_id or not owner_user_id.strip():
            raise ValueError("definition owner must be a non-blank identity")
        definition_id = new_definition_id()
        draft = DefinitionVersion(
            definition_id=definition_id,
            version=1,
            calculation=calculation,
            # Derived from the CalculationSpec, never an empty default: the
            # declared contract and the runtime-validated surface must agree.
            parameter_contract=parameter_contract
            or derive_parameter_contract(calculation),
            # The A6 declaration is OPTIONAL: a caller that declares nothing
            # gets the byte-identical semantics-free version as before.
            semantics=semantics,
            title=title,
            # A newly created Draft does NOT claim semantic closure merely
            # because a CalculationSpec object exists.  Closure is an explicit
            # transition after the semantic contract has been validated.
            semantic_closed=False,
            created_at=utcnow(),
        )
        await self._store.put_definition(
            definition=CustomDefinition(
                definition_id=definition_id,
                owner_user_id=owner_user_id,
                axes=DefinitionAxes(),
                current_version=draft,
            )
        )
        await self._store.put_lifecycle(
            definition_id=definition_id,
            version=draft.version,
            lifecycle=DefinitionVersionLifecycle(),
        )
        return draft

    def _resolve_update(
        self,
        draft: DefinitionVersion,
        *,
        calculation: CalculationSpec | None,
        parameter_contract: ParameterContract | None,
        title: str | None,
        semantics: DefinitionSemantics | None,
    ) -> _ResolvedDraftUpdate:
        """Resolve a draft edit into a fully validated NEW state plus its A6 diff.

        PURE: it reads the draft, computes the intended state and the axis diff,
        and never writes anything.  NOTE: semantics=None means "leave the
        declaration unchanged"; to REMOVE a declared axis the caller passes a
        DefinitionSemantics whose fields are cleared (declaring nothing is
        DIFFERENT from an absent argument).
        """

        new_calculation = calculation if calculation is not None else draft.calculation
        calculation_changed = new_calculation.checksum != draft.calculation.checksum
        if parameter_contract is not None:
            new_parameter_contract = parameter_contract
        elif calculation_changed:
            # Derive from the NEW calculation, never carry the old surface over.
            new_parameter_contract = derive_parameter_contract(new_calculation)
        else:
            new_parameter_contract = draft.parameter_contract
        new_title = title if title is not None else draft.title
        new_semantics = semantics if semantics is not None else draft.semantics
        # A6: the material axis diff between the OLD declaration and the NEW one.
        # It is the ONE decision input to the version boundary -- the six
        # lifecycle axes and the execution binding never enter it.
        axes = semantic_axes(
            _declaration_of(draft),
            SemanticDeclaration(
                expression_checksum=new_calculation.checksum,
                parameter_contract_checksum=new_parameter_contract.checksum,
                semantics=new_semantics,
            ),
        )
        return _ResolvedDraftUpdate(
            calculation=new_calculation,
            parameter_contract=new_parameter_contract,
            title=new_title,
            semantics=new_semantics,
            semantic_axes=axes,
        )

    async def preview_semantic_axes(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        calculation: CalculationSpec | None = None,
        parameter_contract: ParameterContract | None = None,
        semantics: DefinitionSemantics | None = None,
    ) -> tuple[SemanticAxis, ...]:
        """The A6 axes a DRAFT edit WOULD change, without mutating anything.

        A caller can ask BEFORE applying an edit whether the change is
        substantive and therefore needs a NEW business decision.  This is the
        exposure the service provides; it implements no approval workflow.
        """

        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        return self._resolve_update(
            current.current_version,
            calculation=calculation,
            parameter_contract=parameter_contract,
            title=None,
            semantics=semantics,
        ).semantic_axes

    async def update_draft_with_semantics(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        calculation: CalculationSpec | None = None,
        parameter_contract: ParameterContract | None = None,
        title: str | None = None,
        semantics: DefinitionSemantics | None = None,
    ) -> DraftSemanticUpdate:
        """Apply a DRAFT edit and report the A6 axes it changed.

        A6 boundary: a SUBSTANTIVE change (any semantic axis differs) opens a NEW
        draft version and invalidates the closure proof, so the caller must make
        a NEW business decision before confirmation can succeed.  A
        NON-substantive change (no axis differs, for example a title-only edit)
        updates IN PLACE and PRESERVES the existing closure.  A confirmed version
        is immutable: any edit requires a new version through the positive
        revision API.
        """

        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        if current.axes.confirmation == "CONFIRMED":
            # A confirmed/saved version is immutable.  A material semantic edit
            # requires a NEW immutable DefinitionVersion; the positive revision
            # API is NOT implemented in this slice.
            raise ValueError("confirmed definition requires a new version")
        # The current version IS the mutable draft while the definition is not
        # confirmed, so there is no second draft map to keep in sync.
        draft = current.current_version
        resolved = self._resolve_update(
            draft,
            calculation=calculation,
            parameter_contract=parameter_contract,
            title=title,
            semantics=semantics,
        )
        material = bool(resolved.semantic_axes)
        # An IN-PLACE (non-substantive) edit keeps the stored declaration BYTE
        # FOR BYTE, so a diff-equivalent argument (an all-None
        # DefinitionSemantics against an absent one) can never churn the identity
        # of a version that is not supposed to change.
        stored_semantics = resolved.semantics if material else draft.semantics
        # Validate BEFORE storing, so a rejected update leaves no partial state.
        updated = DefinitionVersion(
            definition_id=draft.definition_id,
            # A6: a substantive change opens the NEXT draft version; an
            # in-place edit keeps the current one.
            version=draft.version + 1 if material else draft.version,
            calculation=resolved.calculation,
            parameter_contract=resolved.parameter_contract,
            title=resolved.title,
            semantics=stored_semantics,
            # Closure is INVALIDATED by a material semantic change, and is
            # PRESERVED by a non-substantive one.  It is never client-supplied.
            semantic_closed=False if material else draft.semantic_closed,
            # Draft lineage is preserved verbatim: the new version NUMBER
            # records the supersession, and a fork's source link is never
            # silently rewritten.
            derived_from_definition_id=draft.derived_from_definition_id,
            derived_from_version=draft.derived_from_version,
            created_at=utcnow() if material else draft.created_at,
        )
        await self._store.put_definition(
            definition=current.model_copy(update={"current_version": updated})
        )
        return DraftSemanticUpdate(
            version=updated,
            semantic_axes=resolved.semantic_axes,
            version_created=material,
            requires_business_decision=material,
        )

    async def update_draft(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        calculation: CalculationSpec | None = None,
        parameter_contract: ParameterContract | None = None,
        title: str | None = None,
        semantics: DefinitionSemantics | None = None,
    ) -> DefinitionVersion:
        """Backwards-compatible facade over update_draft_with_semantics.

        The version boundary is IDENTICAL; callers that need the A6 axis diff use
        update_draft_with_semantics or preview_semantic_axes.
        """

        outcome = await self.update_draft_with_semantics(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            calculation=calculation,
            parameter_contract=parameter_contract,
            title=title,
            semantics=semantics,
        )
        return outcome.version

    async def confirm(
        self, *, owner_user_id: str, definition_id: str
    ) -> CustomDefinition:
        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        draft = current.current_version
        # Confirming an UNCLOSED draft would create an immutable version that can
        # never become SAVED (closure refuses confirmed definitions), i.e. a
        # permanent dead end.  Require closure first.
        if not draft.semantic_closed:
            raise ValueError("confirm requires semantic closure")
        published_version = draft.model_copy(update={"created_at": utcnow()})
        await self._store.put_version(version=published_version)
        # The EXACT version's private lifecycle is recorded independently of the
        # current-axis projection, so it survives a later revision.
        await self._store.put_lifecycle(
            definition_id=definition_id,
            version=published_version.version,
            lifecycle=DefinitionVersionLifecycle(
                confirmation="CONFIRMED", retention="SESSION"
            ),
        )
        confirmed = current.model_copy(
            update={
                "axes": current.axes.model_copy(update={"confirmation": "CONFIRMED"}),
                "current_version": published_version,
            }
        )
        await self._store.put_definition(definition=confirmed)
        return confirmed

    async def save(
        self, *, owner_user_id: str, definition_id: str
    ) -> CustomDefinition:
        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        if current.axes.confirmation != "CONFIRMED":
            raise ValueError("SAVED requires CONFIRMED")
        if not current.current_version.semantic_closed:
            raise ValueError("SAVED requires semantic closure")
        saved = current.model_copy(
            update={"axes": current.axes.model_copy(update={"retention": "SAVED"})}
        )
        await self._store.put_definition(definition=saved)
        await self._store.put_lifecycle(
            definition_id=definition_id,
            version=current.current_version.version,
            lifecycle=DefinitionVersionLifecycle(
                confirmation="CONFIRMED", retention="SAVED"
            ),
        )
        return saved

    async def mark_semantic_closed(
        self, *, owner_user_id: str, definition_id: str
    ) -> DefinitionVersion:
        """Explicit semantic-closure transition for a mutable DRAFT.

        Internal service method: a caller must prove the semantic contract is
        closed (valid spec, all required inputs bound, no unresolved business
        slot).  There is deliberately no client-trusted boolean on the wire.
        """

        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        if current.axes.confirmation == "CONFIRMED":
            raise ValueError("a confirmed version is immutable")
        draft = current.current_version
        # Closure is VALIDATED, not a blind boolean flip.
        if draft.parameter_contract.parameters != tuple(draft.calculation.parameters):
            raise ValueError("semantic closure requires a coherent parameter contract")
        for semantic_input in draft.calculation.inputs:
            if semantic_input.provenance == "ad_hoc_metric":
                # A run-scoped NONCANONICAL input must never become reusable
                # authority silently.
                raise ValueError(
                    "semantic closure rejects a run-scoped noncanonical input"
                )
            if (
                semantic_input.provenance == "published_gold"
                and (
                    semantic_input.metric_key is None
                    or not self._is_governed_metric_key(semantic_input.metric_key)
                )
            ):
                raise ValueError(
                    "semantic closure requires an existing eligible published_gold metric"
                )
            if semantic_input.provenance in {
                "definition_backed_computed",
                "product_published_state",
            }:
                raise ValueError(
                    "semantic closure provenance authority is unavailable"
                )
        if draft.semantic_closed:
            return draft
        closed = draft.model_copy(update={"semantic_closed": True})
        await self._store.put_definition(
            definition=current.model_copy(update={"current_version": closed})
        )
        return closed

    def _is_governed_metric_key(self, metric_key: str) -> bool:
        resolver = self._governed_metric_key_resolver
        return (
            resolver(metric_key)
            if resolver is not None
            else metric_key in self._governed_metric_keys
        )


    async def project_published(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> None:
        """Project PUBLISHED onto the CURRENT axes (certification unchanged).

        Only the CURRENT exact version is projected; historical publication state
        lives in the catalogue and is never inferred from these axes.
        """

        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        if current.current_version.version != version:
            return
        axes = current.axes
        if axes.confirmation != "CONFIRMED" or axes.retention != "SAVED":
            raise ValueError("publication projection requires CONFIRMED and SAVED")
        await self._store.put_definition(
            definition=current.model_copy(
                update={
                    "axes": axes.model_copy(
                        update={
                            "publication": "PUBLISHED",
                            "certification": "UNCERTIFIED",
                        }
                    )
                }
            )
        )

    async def project_certified(
        self, *, owner_user_id: str, definition_id: str
    ) -> None:
        """Project CERTIFIED onto the CURRENT axes of a PUBLISHED version."""

        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        if current.axes.publication != "PUBLISHED":
            raise ValueError("certification projection requires PUBLISHED")
        await self._store.put_definition(
            definition=current.model_copy(
                update={
                    "axes": current.axes.model_copy(
                        update={"certification": "CERTIFIED"}
                    )
                }
            )
        )


    async def create_revision(
        self, *, owner_user_id: str, definition_id: str
    ) -> DefinitionVersion:
        """Open a NEW mutable Draft revision of a confirmed definition.

        Semantics: a Confirmed/Saved vN stays IMMUTABLE; revision vN+1 reuses the
        same stable definition identity.  The new revision starts as DRAFT with
        SESSION retention and semantic_closed=False - the closure proof of vN
        never covers vN+1.  Publication/certification are NOT inherited as
        authority, and lineage to vN is explicit.
        """

        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        if current.axes.confirmation != "CONFIRMED":
            raise ValueError("revision requires a confirmed definition")
        prior = current.current_version
        next_version = prior.version + 1
        revision = DefinitionVersion(
            definition_id=definition_id,
            version=next_version,
            calculation=prior.calculation,
            parameter_contract=prior.parameter_contract,
            # A revision STARTS from the prior version's declaration.  Dropping
            # the declared semantics here would be a silent substantive change
            # with no axis recorded at the version boundary.
            semantics=prior.semantics,
            title=prior.title,
            semantic_closed=False,
            derived_from_definition_id=prior.definition_id,
            derived_from_version=prior.version,
            created_at=utcnow(),
        )
        await self._store.put_definition(
            definition=current.model_copy(
                update={
                    # A new revision starts UNPUBLISHED and UNCERTIFIED, whatever
                    # the prior version's publication/certification history was.
                    "axes": DefinitionAxes(),
                    "current_version": revision,
                }
            )
        )
        # vN+1 starts a FRESH private lifecycle; the historical vN record is
        # deliberately left untouched.
        await self._store.put_lifecycle(
            definition_id=definition_id,
            version=revision.version,
            lifecycle=DefinitionVersionLifecycle(),
        )
        return revision


    async def create_fork(
        self,
        *,
        owner_user_id: str,
        title: str,
        calculation: CalculationSpec,
        source_definition_id: str,
        source_version: int,
    ) -> DefinitionVersion:
        """Create a NEW private Definition derived from a published source.

        A fork is a first-class PRIVATE draft, never a copy of the source's
        lifecycle: it starts DRAFT/SESSION/UNPUBLISHED/UNCERTIFIED with
        semantic_closed=False, and it inherits neither Star nor certification.
        """

        definition_id = new_definition_id()
        fork = DefinitionVersion(
            definition_id=definition_id,
            version=1,
            calculation=calculation,
            parameter_contract=derive_parameter_contract(calculation),
            title=title,
            semantic_closed=False,
            derived_from_definition_id=source_definition_id,
            derived_from_version=source_version,
            created_at=utcnow(),
        )
        await self._store.put_definition(
            definition=CustomDefinition(
                definition_id=definition_id,
                owner_user_id=owner_user_id,
                axes=DefinitionAxes(),
                current_version=fork,
            )
        )
        await self._store.put_lifecycle(
            definition_id=definition_id,
            version=fork.version,
            lifecycle=DefinitionVersionLifecycle(),
        )
        return fork

    async def list_owned(
        self, *, owner_user_id: str
    ) -> tuple[CustomDefinition, ...]:
        return tuple(
            definition
            for definition in await self._store.list_definitions()
            if definition.owner_user_id == owner_user_id
        )

    async def list_saved_versions(
        self, *, owner_user_id: str, definition_id: str
    ) -> tuple[DefinitionVersion, ...]:
        """Return exact immutable SAVED versions, including historical ones."""

        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        versions: dict[int, DefinitionVersion] = {}
        for exact in await self._store.list_versions(definition_id=definition_id):
            lifecycle = await self._store.get_lifecycle(
                definition_id=definition_id, version=exact.version
            )
            if lifecycle is not None and lifecycle.retention == "SAVED":
                versions[exact.version] = exact
        current_lifecycle = await self._store.get_lifecycle(
            definition_id=definition_id, version=current.current_version.version
        )
        if current_lifecycle is not None and current_lifecycle.retention == "SAVED":
            versions[current.current_version.version] = current.current_version
        return tuple(versions[key] for key in sorted(versions))

    async def get_exact_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> DefinitionVersion:
        """The exact version, whether it is still a DRAFT or already confirmed.

        The mutable draft is itself a real version of the identity: the product
        UI must be able to read and address it before it is confirmed.  A version
        that is neither the current draft nor a confirmed version does not exist.
        """

        current = await self._owned(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        exact = await self._store.get_version(
            definition_id=definition_id, version=version
        )
        if exact is not None:
            return exact
        # The current version IS the mutable draft while it is not confirmed, so
        # this fallback covers exactly what the removed draft map covered.
        if current.current_version.version == version:
            return current.current_version
        raise DefinitionNotFound()

    async def execute_version(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
        binding: DefinitionExecutionBinding,
    ) -> DefinitionVersion:
        """Resolve the exact version and verify the run binding matches it.

        The concrete parameter VALUES live in the binding, so executing with new
        values never creates a new definition version.
        """

        exact = await self.get_exact_version(
            owner_user_id=owner_user_id, definition_id=definition_id, version=version
        )
        # The WHOLE binding must correspond to this exact immutable version.
        if binding.definition_id != exact.definition_id:
            raise ValueError("execution binding definition id mismatch")
        if binding.version != exact.version:
            raise ValueError("execution binding version mismatch")
        if binding.definition_checksum != exact.checksum:
            raise ValueError("execution binding checksum mismatch")
        # ...and the nested parameter binding must satisfy the exact spec:
        # unknown, missing, wrongly-typed or disallowed parameter values fail.
        failures = binding.binding.binding_failures(exact.calculation)
        if failures:
            raise ValueError(
                "execution binding parameter failures: " + ",".join(sorted(failures))
            )
        return exact


__all__ = [
    "ArtifactNotFound",
    "CustomDefinitionService",
    "DefinitionNotFound",
    "DraftSemanticUpdate",
]
