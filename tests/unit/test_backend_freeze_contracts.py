from __future__ import annotations

from fastapi.routing import APIRoute

from main import create_app
from src.core.auth.dependencies import _required_nl2sql_permission
from src.core.settings import Settings
from src.nl2sql.artifacts.api_conflicts import (
    PersonalConflictResponse,
    PersonalSelectionResponse,
)
from src.nl2sql.artifacts.api_definitions import (
    DefinitionVersionView,
    DefinitionView,
    ExecuteDefinitionResponse,
)
from src.nl2sql.artifacts.api_library import (
    CatalogueEntryView,
    LibraryEntryView,
    MutationResponse,
)
from src.nl2sql.container import AppContainer
from src.nl2sql.supervisor.schemas import ConflictCandidateBlock, ProvenanceBlock
from src.nl2sql.v2 import CapabilityResponse, QueryRequest, QueryResponse


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        auth_required_permission_invoke="nl2sql:invoke",
        auth_required_permission_stream="nl2sql:stream",
    )


def _sample_path(path: str) -> str:
    return (
        path.replace("{thread_id}", "11111111-1111-1111-1111-111111111111")
        .replace("{definition_id}", "def_" + "a" * 32)
        .replace("{version}", "1")
    )


def test_stock_app_openapi_has_unique_explicitly_authorized_v2_routes() -> None:
    app = create_app()
    spec = app.openapi()
    assert spec["paths"]
    expected_paths = {
        "/api/v2/nl2sql/queries",
        "/api/v2/nl2sql/queries/stream",
        "/api/v2/nl2sql/capabilities",
        "/api/v2/nl2sql/threads/{thread_id}/actions",
        "/api/v2/nl2sql/definitions",
        "/api/v2/nl2sql/definitions/{definition_id}/versions/{version}/execute",
        "/api/v2/nl2sql/library",
        "/api/v2/nl2sql/library/catalogue",
        "/api/v2/nl2sql/conflicts/personal",
        "/api/v2/nl2sql/conflicts/personal/select",
    }
    assert expected_paths <= set(spec["paths"])

    registered: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith(
            "/api/v2/nl2sql"
        ):
            continue
        for method in route.methods or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            registered.append((method, route.path))
            assert (
                _required_nl2sql_permission(
                    method,
                    _sample_path(route.path),
                    _settings(),
                )
                is not None
            ), (method, route.path)
    assert len(registered) == len(set(registered))


def test_frontend_contract_models_expose_frozen_server_owned_fields() -> None:
    for name in (
        "run_id",
        "requested_mode",
        "effective_mode",
        "switched_from_run_id",
        "authority_provenance",
        "blocks",
    ):
        assert name in QueryResponse.model_fields
    for forbidden in ("run_id", "effective_mode", "authority_provenance"):
        assert forbidden not in QueryRequest.model_fields

    assert {
        "supported_block_types",
        "unsupported_block_fallback",
        "typed_runtime",
        "degradation_reasons",
    } <= set(CapabilityResponse.model_fields)
    assert {
        "calculation",
        "semantic_closed",
        "confirmation",
        "retention",
        "publication",
        "certification",
        "derived_from_definition_id",
        "derived_from_version",
    } <= set(DefinitionView.model_fields)
    assert {
        "parameter_contract_checksum",
        "semantic_closed",
        "confirmation",
        "retention",
        "publication",
        "certification",
        "withdrawn",
    } <= set(DefinitionVersionView.model_fields)
    assert {
        "status",
        "definition_id",
        "version",
        "definition_checksum",
        "calculation_id",
        "spec_checksum",
        "binding_checksum",
        "value",
        "unit",
        "input_provenance",
        "data_as_of",
        "time_range",
        "calculation_scope",
    } == set(ExecuteDefinitionResponse.model_fields)

    assert {
        "installed_version",
        "pinned",
        "current_version",
        "update_available",
        "starred",
        "star_count",
        "certification_state",
        "withdrawn",
        "withdrawal_acknowledged",
        "forkable",
        "derived_from_identity",
        "derived_from_version",
    } <= set(LibraryEntryView.model_fields)
    assert {"current_version", "star_count", "versions"} <= set(
        CatalogueEntryView.model_fields
    )
    assert {
        "installed_version",
        "installed",
        "star_count",
        "starred",
        "certification_state",
        "withdrawn",
        "forked_definition_id",
    } <= set(MutationResponse.model_fields)

    assert set(PersonalConflictResponse.model_fields) == {
        "resolution",
        "semantic_conflict",
        "conflict_comparison",
        "selected_candidate_id",
        "selected_definition_id",
        "selected_version",
    }
    assert set(PersonalSelectionResponse.model_fields) == {
        "valid",
        "selection",
        "conflict_comparison",
    }
    forbidden_comparison = {"winner", "recommended", "best", "score", "rank"}
    assert forbidden_comparison.isdisjoint(ConflictCandidateBlock.model_fields)
    assert {
        "evidence_checksum",
        "metric_keys",
        "analysis_window",
        "data_as_of",
        "source_ids",
        "source_checkpoints",
        "fact_ids",
        "authority_provenance",
        "model_provider",
        "model",
    } <= set(ProvenanceBlock.model_fields)


def test_app_container_owns_one_shared_product_object_graph() -> None:
    container = AppContainer()
    definitions = container.custom_definition_service()
    catalogue = container.publication_catalogue()
    publication = container.publication_service()
    library = container.library_repository()
    product_library = container.product_library_service()
    personal_conflict = container.personal_conflict_product_service()
    execution = container.custom_definition_execution_service()

    assert definitions is container.custom_definition_service()
    assert catalogue is container.publication_catalogue()
    assert publication is container.publication_service()
    assert library is container.library_repository()
    assert product_library is container.product_library_service()
    assert personal_conflict is container.personal_conflict_product_service()
    assert execution is container.custom_definition_execution_service()
    assert publication._definitions is definitions
    assert publication._catalogue is catalogue
    assert product_library._definitions is definitions
    assert product_library._catalogue is catalogue
    assert product_library._library is library
    assert personal_conflict._definitions is definitions
    assert personal_conflict._catalogue is catalogue
    assert personal_conflict._library is library
    assert execution._definitions is definitions
    assert execution._input_resolver is None
