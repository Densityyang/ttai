"""Server-owned personal semantic-conflict application service."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.semantic.calculation_contract import SemanticResolution
from src.nl2sql.semantic.personal_conflict_contract import (
    CandidateCertificationState,
    PersonalSelection,
)
from src.nl2sql.semantic.personal_conflict_service import (
    PersonalConflictProjection,
    PersonalConflictServiceError,
    project_installed_published_candidate,
    project_own_saved_candidate,
    resolve_personal_candidate_conflict,
    validate_personal_selection,
)

if TYPE_CHECKING:
    from src.nl2sql.artifacts.ports import CataloguePort, LibraryPort

__all__ = [
    "PersonalConflictProductError",
    "PersonalConflictProductService",
]


class PersonalConflictProductError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PersonalConflictProductService:
    """Resolve exact owner/pinned candidates; client metadata is never trusted."""

    def __init__(
        self,
        *,
        definitions: CustomDefinitionService,
        catalogue: CataloguePort,
        library: LibraryPort,
    ) -> None:
        self._definitions = definitions
        self._catalogue = catalogue
        self._library = library
        # Conflict projections and explicit selections are run-scoped.  They
        # are process-local in this DEMO/LOCAL service, but the key still binds
        # owner + thread + current run + conflict id so a copied selection can
        # never migrate between runs or users.
        self._active_conflicts: dict[
            tuple[str, str, str, str], PersonalConflictProjection
        ] = {}
        self._pending_selections: dict[
            tuple[str, str, str, str], PersonalSelection
        ] = {}

    @staticmethod
    def _binding_key(
        *, user_id: str, thread_id: str, run_id: str, conflict_id: str
    ) -> tuple[str, str, str, str]:
        return (user_id, thread_id, run_id, conflict_id)

    async def resolve(
        self,
        *,
        user_id: str,
        own_definition_id: str,
        installed_identity_id: str,
        thread_id: str | None = None,
        run_id: str | None = None,
        own_version: int | None = None,
    ) -> PersonalConflictProjection:
        await self._definitions.get_owned_definition(
            owner_user_id=user_id,
            definition_id=own_definition_id,
        )
        saved_versions = await self._definitions.list_saved_versions(
            owner_user_id=user_id,
            definition_id=own_definition_id,
        )
        if not saved_versions:
            raise PersonalConflictProductError("personal_candidate_not_saved_confirmed")
        if own_version is None:
            own_version = saved_versions[-1].version
        if own_version not in {item.version for item in saved_versions}:
            raise PersonalConflictProductError("personal_candidate_version_unavailable")
        own_version_record = await self._definitions.get_exact_version(
            owner_user_id=user_id,
            definition_id=own_definition_id,
            version=own_version,
        )
        lifecycle = await self._definitions.get_version_lifecycle(
            owner_user_id=user_id,
            definition_id=own_definition_id,
            version=own_version_record.version,
        )
        install = await self._library.get_install(
            user_id=user_id,
            identity_id=installed_identity_id,
        )
        if install is None:
            raise PersonalConflictProductError("personal_conflict_install_required")
        published = await self._catalogue.get(installed_identity_id, install.version)
        if published is None:
            raise PersonalConflictProductError("personal_conflict_publication_not_found")
        certification_raw = await self._catalogue.certification_state(
            installed_identity_id,
            install.version,
        )
        certification = (
            certification_raw
            if certification_raw in {"unknown", "uncertified", "certified"}
            else "unknown"
        )
        try:
            own = project_own_saved_candidate(
                version=own_version_record,
                lifecycle=lifecycle,
                owner_user_id=user_id,
                owner_label=user_id,
            )
            installed = project_installed_published_candidate(
                published=published,
                certification_state=cast(
                    CandidateCertificationState, certification
                ),
                star_count=await self._library.star_count(
                    identity_id=installed_identity_id
                ),
            )
            projection = resolve_personal_candidate_conflict((own, installed))
            if projection.semantic_conflict is not None and thread_id and run_id:
                conflict = projection.semantic_conflict
                self._active_conflicts[
                    self._binding_key(
                        user_id=user_id,
                        thread_id=thread_id,
                        run_id=run_id,
                        conflict_id=conflict.conflict_id,
                    )
                ] = projection
                pending = self._pending_selections.get(
                    self._binding_key(
                        user_id=user_id,
                        thread_id=thread_id,
                        run_id=run_id,
                        conflict_id=conflict.conflict_id,
                    )
                )
                if pending is not None:
                    consumed = await self.consume_selection(
                        user_id=user_id,
                        thread_id=thread_id,
                        run_id=run_id,
                        conflict_id=conflict.conflict_id,
                    )
                    selected_reference = next(
                        item
                        for item in conflict.candidates
                        if item.candidate_id == consumed.selected_candidate_id
                    )
                    return projection.model_copy(
                        update={
                            "resolution": SemanticResolution(outcome="resolved"),
                            "semantic_conflict": None,
                            "conflict_comparison": None,
                            "selected_candidate_id": consumed.selected_candidate_id,
                            "selected_definition_id": selected_reference.definition_id,
                            "selected_version": selected_reference.definition_version,
                        }
                    )
            return projection
        except PersonalConflictServiceError as exc:
            raise PersonalConflictProductError(exc.code) from exc

    async def validate_selection(
        self,
        *,
        user_id: str,
        own_definition_id: str,
        installed_identity_id: str,
        selection: PersonalSelection,
        thread_id: str | None = None,
        run_id: str | None = None,
        own_version: int | None = None,
    ) -> tuple[PersonalConflictProjection, PersonalSelection]:
        if selection.selection_scope != "run_scoped":
            raise PersonalConflictProductError(
                "personal_selection_must_be_run_scoped"
            )
        if thread_id is not None and run_id is not None:
            projection = self._active_conflicts.get(
                self._binding_key(
                    user_id=user_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    conflict_id=selection.conflict_id,
                )
            )
            if projection is None:
                raise PersonalConflictProductError(
                    "personal_selection_run_binding_invalid"
                )
        else:
            # Direct service callers from the pure contract tests may omit a
            # transport binding.  The HTTP product route never does so.
            projection = await self.resolve(
                user_id=user_id,
                own_definition_id=own_definition_id,
                installed_identity_id=installed_identity_id,
                own_version=own_version,
            )
        try:
            validated = validate_personal_selection(
                selection,
                projection=projection,
            )
        except PersonalConflictServiceError as exc:
            raise PersonalConflictProductError(exc.code) from exc
        if thread_id is not None and run_id is not None:
            self._pending_selections[
                self._binding_key(
                    user_id=user_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    conflict_id=validated.conflict_id,
                )
            ] = validated
        return projection, validated

    async def consume_selection(
        self,
        *,
        user_id: str,
        thread_id: str,
        run_id: str,
        conflict_id: str,
    ) -> PersonalSelection:
        """Consume the exact selection once for subsequent semantic resolution."""

        key = self._binding_key(
            user_id=user_id,
            thread_id=thread_id,
            run_id=run_id,
            conflict_id=conflict_id,
        )
        selection = self._pending_selections.pop(key, None)
        if selection is None:
            raise PersonalConflictProductError("personal_selection_not_pending")
        self._active_conflicts.pop(key, None)
        return selection
