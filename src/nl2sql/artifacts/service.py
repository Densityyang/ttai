"""Custom Definition service: the BUILD vertical flow (DEMO/local, non-durable).

Owner identity is ALWAYS supplied by the caller from the authenticated request
context.  A cross-user access raises the same not-found as an absent definition.
"""

from __future__ import annotations

from collections.abc import Callable, Collection

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
from src.nl2sql.artifacts.repository import ArtifactNotFound
from src.nl2sql.semantic.calculation_contract import CalculationSpec


def _default_governed_metric_keys() -> frozenset[str]:
    """Fail closed when no applicable server-owned authority is injected."""

    return frozenset()


class DefinitionNotFound(LookupError):
    def __init__(self) -> None:
        super().__init__("definition_not_found")


class CustomDefinitionService:
    """Process-local definition store with immutable versions.

    DRAFT edits happen inside the draft working copy.  Once a CONFIRMED/SAVED
    version exists, a material semantic edit creates the NEXT version - an
    existing version is never mutated in place.
    """

    def __init__(
        self,
        *,
        governed_metric_key_resolver: Callable[[str], bool] | None = None,
        governed_metric_keys: Collection[str] | None = None,
    ) -> None:
        self._definitions: dict[str, CustomDefinition] = {}
        self._drafts: dict[str, DefinitionVersion] = {}
        self._versions: dict[tuple[str, int], DefinitionVersion] = {}
        # EXACT per-version private lifecycle, keyed by (definition_id, version).
        # It is never reset by a later revision, so historical publication
        # eligibility stays provable for an EXACT version.
        self._lifecycle: dict[tuple[str, int], DefinitionVersionLifecycle] = {}
        if governed_metric_key_resolver is not None and governed_metric_keys is not None:
            raise ValueError("provide a metric resolver or a metric set, not both")
        self._governed_metric_key_resolver = governed_metric_key_resolver
        self._governed_metric_keys = frozenset(
            governed_metric_keys
            if governed_metric_keys is not None
            else _default_governed_metric_keys()
        )

    # --- helpers ---------------------------------------------------------
    def _owned(self, *, owner_user_id: str, definition_id: str) -> CustomDefinition:
        definition = self._definitions.get(definition_id)
        if definition is None or definition.owner_user_id != owner_user_id:
            raise DefinitionNotFound()
        return definition

    # --- bounded PUBLIC owner-scoped API ---------------------------------
    def get_owned_definition(
        self, *, owner_user_id: str, definition_id: str
    ) -> CustomDefinition:
        """Own public accessor.  Foreign == absent, both raise DefinitionNotFound.

        Cross-service callers (Publication, Product Library) MUST use this or
        another public method rather than the private owner-check helper.
        """

        return self._owned(owner_user_id=owner_user_id, definition_id=definition_id)

    def get_version_lifecycle(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> DefinitionVersionLifecycle:
        """The EXACT private lifecycle of one version, never the current axes.

        The owner check runs FIRST, so a foreign caller gets the same failure as
        an absent definition and cannot probe whether a version exists.
        """

        # Reuse the SAME resolution as get_exact_version, so a DRAFT version and
        # a confirmed version are treated consistently by both readers.
        exact = self.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )
        recorded = self._lifecycle.get((definition_id, version))
        if recorded is not None:
            return recorded
        # A version with no explicit lifecycle record is derived from its own
        # immutable state, never from the mutable current axes.
        if exact.semantic_closed and (definition_id, version) in self._versions:
            return DefinitionVersionLifecycle(
                confirmation="CONFIRMED",
                retention="SAVED",
            )
        return DefinitionVersionLifecycle()

    # --- lifecycle -------------------------------------------------------
    def create_draft(
        self,
        *,
        owner_user_id: str,
        title: str,
        calculation: CalculationSpec,
        parameter_contract: ParameterContract | None = None,
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
            title=title,
            # A newly created Draft does NOT claim semantic closure merely
            # because a CalculationSpec object exists.  Closure is an explicit
            # transition after the semantic contract has been validated.
            semantic_closed=False,
            created_at=utcnow(),
        )
        self._drafts[definition_id] = draft
        self._lifecycle[(definition_id, draft.version)] = DefinitionVersionLifecycle()
        self._definitions[definition_id] = CustomDefinition(
            definition_id=definition_id,
            owner_user_id=owner_user_id,
            axes=DefinitionAxes(),
            current_version=draft,
        )
        return draft

    def update_draft(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        calculation: CalculationSpec | None = None,
        parameter_contract: ParameterContract | None = None,
        title: str | None = None,
    ) -> DefinitionVersion:
        current = self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        if current.axes.confirmation == "CONFIRMED":
            # A confirmed/saved version is immutable.  A material semantic edit
            # requires a NEW immutable DefinitionVersion; the positive revision
            # API is NOT implemented in this slice.
            raise ValueError("confirmed definition requires a new version")
        draft = self._drafts[definition_id]
        # Resolve the intended NEW state, then RECONSTRUCT a fully validated
        # DefinitionVersion.  An unvalidated model_copy(update=...) would skip
        # the parameter-coherence validator, so a changed CalculationSpec could
        # silently keep the OLD parameter contract.
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
        # Closure is INVALIDATED by a material semantic change.  The caller must
        # re-prove closure; it can never be preserved or supplied explicitly.
        semantic_inputs_changed = calculation_changed or (
            new_parameter_contract.checksum != draft.parameter_contract.checksum
        )
        new_closed = False if semantic_inputs_changed else draft.semantic_closed
        # Validate BEFORE storing, so a rejected update leaves no partial state.
        updated = DefinitionVersion(
            definition_id=draft.definition_id,
            version=draft.version,
            calculation=new_calculation,
            parameter_contract=new_parameter_contract,
            title=new_title,
            semantic_closed=new_closed,
            derived_from_definition_id=draft.derived_from_definition_id,
            derived_from_version=draft.derived_from_version,
            created_at=draft.created_at,
        )
        self._drafts[definition_id] = updated
        self._definitions[definition_id] = current.model_copy(
            update={"current_version": updated}
        )
        return updated

    def confirm(
        self, *, owner_user_id: str, definition_id: str
    ) -> CustomDefinition:
        current = self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        draft = self._drafts[definition_id]
        # Confirming an UNCLOSED draft would create an immutable version that can
        # never become SAVED (closure refuses confirmed definitions), i.e. a
        # permanent dead end.  Require closure first.
        if not draft.semantic_closed:
            raise ValueError("confirm requires semantic closure")
        published_version = draft.model_copy(update={"created_at": utcnow()})
        self._versions[(definition_id, published_version.version)] = published_version
        # The EXACT version's private lifecycle is recorded independently of the
        # current-axis projection, so it survives a later revision.
        self._lifecycle[(definition_id, published_version.version)] = (
            DefinitionVersionLifecycle(confirmation="CONFIRMED", retention="SESSION")
        )
        confirmed = current.model_copy(
            update={
                "axes": current.axes.model_copy(update={"confirmation": "CONFIRMED"}),
                "current_version": published_version,
            }
        )
        self._definitions[definition_id] = confirmed
        return confirmed

    def save(self, *, owner_user_id: str, definition_id: str) -> CustomDefinition:
        current = self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        if current.axes.confirmation != "CONFIRMED":
            raise ValueError("SAVED requires CONFIRMED")
        if not current.current_version.semantic_closed:
            raise ValueError("SAVED requires semantic closure")
        saved = current.model_copy(
            update={"axes": current.axes.model_copy(update={"retention": "SAVED"})}
        )
        self._definitions[definition_id] = saved
        self._lifecycle[(definition_id, current.current_version.version)] = (
            DefinitionVersionLifecycle(confirmation="CONFIRMED", retention="SAVED")
        )
        return saved

    def mark_semantic_closed(
        self, *, owner_user_id: str, definition_id: str
    ) -> DefinitionVersion:
        """Explicit semantic-closure transition for a mutable DRAFT.

        Internal service method: a caller must prove the semantic contract is
        closed (valid spec, all required inputs bound, no unresolved business
        slot).  There is deliberately no client-trusted boolean on the wire.
        """

        current = self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        if current.axes.confirmation == "CONFIRMED":
            raise ValueError("a confirmed version is immutable")
        draft = self._drafts[definition_id]
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
        self._drafts[definition_id] = closed
        self._definitions[definition_id] = current.model_copy(
            update={"current_version": closed}
        )
        return closed

    def _is_governed_metric_key(self, metric_key: str) -> bool:
        resolver = self._governed_metric_key_resolver
        return (
            resolver(metric_key)
            if resolver is not None
            else metric_key in self._governed_metric_keys
        )


    def project_published(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> None:
        """Project PUBLISHED onto the CURRENT axes (certification unchanged).

        Only the CURRENT exact version is projected; historical publication state
        lives in the catalogue and is never inferred from these axes.
        """

        current = self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        if current.current_version.version != version:
            return
        axes = current.axes
        if axes.confirmation != "CONFIRMED" or axes.retention != "SAVED":
            raise ValueError("publication projection requires CONFIRMED and SAVED")
        self._definitions[definition_id] = current.model_copy(
            update={
                "axes": axes.model_copy(
                    update={"publication": "PUBLISHED", "certification": "UNCERTIFIED"}
                )
            }
        )

    def project_certified(
        self, *, owner_user_id: str, definition_id: str
    ) -> None:
        """Project CERTIFIED onto the CURRENT axes of a PUBLISHED version."""

        current = self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        if current.axes.publication != "PUBLISHED":
            raise ValueError("certification projection requires PUBLISHED")
        self._definitions[definition_id] = current.model_copy(
            update={
                "axes": current.axes.model_copy(
                    update={"certification": "CERTIFIED"}
                )
            }
        )


    def create_revision(
        self, *, owner_user_id: str, definition_id: str
    ) -> DefinitionVersion:
        """Open a NEW mutable Draft revision of a confirmed definition.

        Semantics: a Confirmed/Saved vN stays IMMUTABLE; revision vN+1 reuses the
        same stable definition identity.  The new revision starts as DRAFT with
        SESSION retention and semantic_closed=False - the closure proof of vN
        never covers vN+1.  Publication/certification are NOT inherited as
        authority, and lineage to vN is explicit.
        """

        current = self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        if current.axes.confirmation != "CONFIRMED":
            raise ValueError("revision requires a confirmed definition")
        prior = current.current_version
        next_version = prior.version + 1
        revision = DefinitionVersion(
            definition_id=definition_id,
            version=next_version,
            calculation=prior.calculation,
            parameter_contract=prior.parameter_contract,
            title=prior.title,
            semantic_closed=False,
            derived_from_definition_id=prior.definition_id,
            derived_from_version=prior.version,
            created_at=utcnow(),
        )
        self._drafts[definition_id] = revision
        # vN+1 starts a FRESH private lifecycle; the historical vN record is
        # deliberately left untouched.
        self._lifecycle[(definition_id, revision.version)] = DefinitionVersionLifecycle()
        self._definitions[definition_id] = current.model_copy(
            update={
                # A new revision starts UNPUBLISHED and UNCERTIFIED, whatever the
                # prior version's publication/certification history was.
                "axes": DefinitionAxes(),
                "current_version": revision,
            }
        )
        return revision


    def create_fork(
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
        self._drafts[definition_id] = fork
        self._lifecycle[(definition_id, fork.version)] = DefinitionVersionLifecycle()
        self._definitions[definition_id] = CustomDefinition(
            definition_id=definition_id,
            owner_user_id=owner_user_id,
            axes=DefinitionAxes(),
            current_version=fork,
        )
        return fork

    def list_owned(self, *, owner_user_id: str) -> tuple[CustomDefinition, ...]:
        return tuple(
            definition
            for definition in self._definitions.values()
            if definition.owner_user_id == owner_user_id
        )

    def list_saved_versions(
        self, *, owner_user_id: str, definition_id: str
    ) -> tuple[DefinitionVersion, ...]:
        """Return exact immutable SAVED versions, including historical ones."""

        self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        versions: dict[int, DefinitionVersion] = {}
        for (candidate_id, version), exact in self._versions.items():
            if candidate_id != definition_id:
                continue
            lifecycle = self._lifecycle.get((candidate_id, version))
            if lifecycle is not None and lifecycle.retention == "SAVED":
                versions[version] = exact
        current = self._definitions[definition_id].current_version
        current_lifecycle = self._lifecycle.get((definition_id, current.version))
        if current_lifecycle is not None and current_lifecycle.retention == "SAVED":
            versions[current.version] = current
        return tuple(versions[key] for key in sorted(versions))

    def get_exact_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> DefinitionVersion:
        """The exact version, whether it is still a DRAFT or already confirmed.

        The mutable draft is itself a real version of the identity: the product
        UI must be able to read and address it before it is confirmed.  A version
        that is neither the current draft nor a confirmed version does not exist.
        """

        current = self._owned(owner_user_id=owner_user_id, definition_id=definition_id)
        exact = self._versions.get((definition_id, version))
        if exact is None:
            draft = self._drafts.get(definition_id)
            if draft is not None and draft.version == version:
                return draft
            if current.current_version.version == version:
                return current.current_version
            raise DefinitionNotFound()
        return exact

    def execute_version(
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

        exact = self.get_exact_version(
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
]
