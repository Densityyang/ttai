"""Focused tests for the artifact / definition / library product foundation."""

from __future__ import annotations

import pytest

from src.nl2sql.artifacts.contracts import (
    AnalysisArtifact,
    CustomDefinitionArtifact,
)
from src.nl2sql.artifacts.custom_definition import (
    DefinitionExecutionBinding,
    DefinitionVersion,
    ParameterContract,
)
from src.nl2sql.artifacts.library import (
    NOT_CONNECTED,
    InMemoryLibraryRepository,
    LibraryIdentityNotFound,
)
from src.nl2sql.artifacts.publication import (
    PUBLICATION_WITHDRAWN,
    PublicationCatalogue,
    PublishedSemanticPackage,
    PublishedVersion,
)
from src.nl2sql.artifacts.repository import (
    ArtifactNotFound,
    ArtifactTypeMismatch,
    InMemoryArtifactRepository,
)
from src.nl2sql.artifacts.service import (
    CustomDefinitionService,
    DefinitionNotFound,
)
from src.nl2sql.demo.fixtures import DemoPublishedVersion
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
    ParameterBinding,
    ParameterSpec,
)

IDENTITY = "demo.metric.margin"

V1 = DemoPublishedVersion(identity_id=IDENTITY, version=1, value="0.37", unit="ratio", certification_state="certified")
V2 = DemoPublishedVersion(identity_id=IDENTITY, version=2, value="0.41", unit="ratio")
V99 = DemoPublishedVersion(identity_id=IDENTITY, version=99, value="9.9", unit="ratio")


def _spec(calculation_id: str = "calc.demo") -> CalculationSpec:
    """A REAL parameterized spec, so parameter binding is exercised."""
    return CalculationSpec(
        calculation_id=calculation_id,
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key="demo.revenue"
            ),
        ),
        unit="count",
        parameters=(
            ParameterSpec(
                name="threshold",
                value_type="integer",
                required=True,
                allowed_values=("1", "2"),
            ),
        ),
    )


def _binding(exact, **overrides: object) -> DefinitionExecutionBinding:
    base: dict[str, object] = {
        "definition_id": exact.definition_id,
        "version": exact.version,
        "definition_checksum": exact.checksum,
        "binding": CalculationExecutionBinding(
            calculation_id=exact.calculation.calculation_id,
            spec_checksum=exact.calculation.checksum,
            parameters=(ParameterBinding(name="threshold", value=1),),
        ),
    }
    base.update(overrides)
    return DefinitionExecutionBinding(**base)  # type: ignore[arg-type]

# --- artifact ----------------------------------------------------------------


def test_artifact_owner_isolation() -> None:
    repo = InMemoryArtifactRepository()
    record = repo.create(
        owner_user_id="alice",
        payload=AnalysisArtifact(title="t", summary="s"),
    )
    assert record.owner_user_id == "alice"
    assert record.artifact_id.startswith("art_")
    assert repo.list_for_owner(owner_user_id="alice") == (record,)
    assert repo.list_for_owner(owner_user_id="bob") == ()
    # a foreign reader gets the SAME failure as a never-existing id
    absent = "art_" + "0" * 32
    with pytest.raises(ArtifactNotFound):
        repo.get(owner_user_id="bob", artifact_id=record.artifact_id)
    with pytest.raises(ArtifactNotFound):
        repo.get(owner_user_id="bob", artifact_id=absent)


def test_artifact_ids_are_server_minted_and_unique() -> None:
    repo = InMemoryArtifactRepository()
    ids = {
        repo.create(
            owner_user_id="alice",
            payload=AnalysisArtifact(title="t", summary="s"),
        ).artifact_id
        for _ in range(5)
    }
    assert len(ids) == 5


def test_artifact_replacement_is_type_preserving() -> None:
    repo = InMemoryArtifactRepository()
    analysis = repo.create(
        owner_user_id="alice",
        payload=AnalysisArtifact(title="t", summary="s"),
    )
    updated = repo.replace_payload(
        owner_user_id="alice",
        artifact_id=analysis.artifact_id,
        payload=AnalysisArtifact(title="t2", summary="s2"),
    )
    assert updated.artifact_type == "analysis"
    with pytest.raises(ArtifactTypeMismatch):
        repo.replace_payload(
            owner_user_id="alice",
            artifact_id=analysis.artifact_id,
            payload=CustomDefinitionArtifact(
                definition_id="def_x",
                definition_version=1,
                definition_checksum="a" * 64,
            ),
        )
    definition = repo.create(
        owner_user_id="alice",
        payload=CustomDefinitionArtifact(
            definition_id="def_y", definition_version=1, definition_checksum="b" * 64
        ),
    )
    assert definition.artifact_type == "custom_definition"
    with pytest.raises(ArtifactTypeMismatch):
        repo.replace_payload(
            owner_user_id="alice",
            artifact_id=definition.artifact_id,
            payload=AnalysisArtifact(title="x", summary="y"),
        )


# --- custom definition -------------------------------------------------------


def test_draft_starts_not_semantically_closed() -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    assert draft.semantic_closed is False


def test_save_requires_explicit_semantic_closure() -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    with pytest.raises(ValueError):
        service.save(owner_user_id="alice", definition_id=draft.definition_id)
    closed = service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    assert closed.semantic_closed is True
    service.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    saved = service.save(owner_user_id="alice", definition_id=draft.definition_id)
    assert saved.axes.retention == "SAVED"


def test_cross_user_semantic_closure_matches_absent() -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    with pytest.raises(DefinitionNotFound):
        service.mark_semantic_closed(
            owner_user_id="bob", definition_id=draft.definition_id
        )
    with pytest.raises(DefinitionNotFound):
        service.mark_semantic_closed(
            owner_user_id="bob", definition_id="def_" + "0" * 32
        )


def _closed_service():
    """A confirmed+saved definition built from a REAL parameterized spec."""
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    service.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    service.save(owner_user_id="alice", definition_id=draft.definition_id)
    exact = service.get_exact_version(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )
    return service, draft.definition_id, exact


def test_execute_version_validates_the_whole_binding() -> None:
    service, definition_id, exact = _closed_service()
    good = _binding(exact)
    assert (
        service.execute_version(
            owner_user_id="alice",
            definition_id=definition_id,
            version=1,
            binding=good,
        ).version
        == 1
    )
    rejects = {
        "wrong version": good.model_copy(update={"version": 2}),
        "wrong definition id": good.model_copy(
            update={"definition_id": "def_" + "0" * 32}
        ),
        "wrong checksum": good.model_copy(update={"definition_checksum": "0" * 64}),
    }
    for label, bad in rejects.items():
        with pytest.raises(ValueError):
            service.execute_version(
                owner_user_id="alice",
                definition_id=definition_id,
                version=1,
                binding=bad,
            )
        del label


def test_real_parameter_binding_values_and_failures() -> None:
    """Exercises CalculationExecutionBinding.binding_failures via the service."""
    service, definition_id, exact = _closed_service()
    calculation_id = exact.calculation.calculation_id
    spec_checksum = exact.calculation.checksum

    def _with(*, parameters, calculation_id_=calculation_id, spec_checksum_=spec_checksum):
        return _binding(
            exact,
            binding=CalculationExecutionBinding(
                calculation_id=calculation_id_,
                spec_checksum=spec_checksum_,
                parameters=parameters,
            ),
        )

    # a DIFFERENT VALID value executes the SAME version
    for value in (1, 2):
        binding = _with(
            parameters=(ParameterBinding(name="threshold", value=value),)
        )
        assert (
            service.execute_version(
                owner_user_id="alice",
                definition_id=definition_id,
                version=1,
                binding=binding,
            ).version
            == 1
        )

    failures = {
        "missing required": (),
        "unknown parameter": (ParameterBinding(name="nope", value=1),),
        # 9 is a valid integer but NOT in allowed_values
        "disallowed value": (ParameterBinding(name="threshold", value=9),),
        # a string is the wrong type for an integer parameter
        "wrong type": (ParameterBinding(name="threshold", value="abc"),),
    }
    for label, parameters in failures.items():
        with pytest.raises(ValueError):
            service.execute_version(
                owner_user_id="alice",
                definition_id=definition_id,
                version=1,
                binding=_with(parameters=parameters),
            )
        del label
    # wrong calculation id / spec checksum are also rejected
    with pytest.raises(ValueError):
        service.execute_version(
            owner_user_id="alice",
            definition_id=definition_id,
            version=1,
            binding=_with(
                parameters=(ParameterBinding(name="threshold", value=1),),
                calculation_id_="calc.other",
            ),
        )
    with pytest.raises(ValueError):
        service.execute_version(
            owner_user_id="alice",
            definition_id=definition_id,
            version=1,
            binding=_with(
                parameters=(ParameterBinding(name="threshold", value=1),),
                spec_checksum_="0" * 64,
            ),
        )
    # and the definition version itself is unchanged by all of the above
    after = service.get_exact_version(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    assert after.checksum == exact.checksum


def test_new_parameter_values_do_not_create_a_definition_version() -> None:
    service, definition_id, exact = _closed_service()
    before = service.list_owned(owner_user_id="alice")[0]
    service.execute_version(
        owner_user_id="alice",
        definition_id=definition_id,
        version=1,
        binding=_binding(
            exact,
            binding=CalculationExecutionBinding(
                calculation_id=exact.calculation.calculation_id,
                spec_checksum=exact.calculation.checksum,
                parameters=(ParameterBinding(name="threshold", value=2),),
            ),
        ),
    )
    after = service.list_owned(owner_user_id="alice")[0]
    assert after.current_version.version == before.current_version.version == 1
    assert after.current_version.checksum == before.current_version.checksum


def test_parameter_contract_derives_from_the_calculation_spec() -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    assert draft.parameter_contract.parameters == tuple(_spec().parameters)
    assert len(draft.parameter_contract.parameters) == 1


# --- library -----------------------------------------------------------------


def _catalogue_repo(
    versions: tuple[DemoPublishedVersion, ...] = (V1, V2),
    *,
    current_versions: dict[str, int] | None = None,
    certifications: tuple[tuple[str, int], ...] = ((IDENTITY, 1),),
) -> InMemoryLibraryRepository:
    """Build the repository over a REAL shared catalogue (no second map)."""

    catalogue = PublicationCatalogue()
    current_map = current_versions or {IDENTITY: 2}
    ordered_versions = tuple(
        sorted(
            versions,
            key=lambda fixture: (
                0 if current_map.get(fixture.identity_id) == fixture.version else 1
            ),
        )
    )
    for fixture in ordered_versions:
        catalogue.seed(
            _fixture_publication(fixture),
            current=current_map.get(fixture.identity_id) == fixture.version,
        )
    for identity_id, version in certifications:
        catalogue.certify_local_demo(identity_id, version, certified_by="fixture")
    return InMemoryLibraryRepository(catalogue=catalogue)


def _fixture_publication(fixture: DemoPublishedVersion) -> PublishedVersion:
    return PublishedVersion(
        identity_id=fixture.identity_id,
        version=fixture.version,
        title=fixture.identity_id,
        owner_user_id="fixture",
        owner_label="fixture",
        source_label="fixture",
        definition_checksum="0" * 64,
        published_at="fixture",
        unit=fixture.unit,
        value=fixture.value,
    )


def _semantic_publication(version: int) -> PublishedVersion:
    calculation = _spec(f"calc.seed.{version}")
    return PublishedVersion(
        identity_id=IDENTITY,
        version=version,
        title=IDENTITY,
        owner_user_id="fixture",
        owner_label="fixture",
        source_label="fixture",
        definition_checksum="a" * 64,
        published_at="fixture",
        semantic=PublishedSemanticPackage(
            calculation=calculation,
            parameter_contract=ParameterContract(
                parameters=tuple(calculation.parameters)
            ),
            source_definition_id="def_" + ("a" * 32),
            source_definition_version=version,
            source_definition_checksum="b" * 64,
        ),
    )


def test_library_current_pointer_is_authoritative_over_a_newer_version() -> None:
    """A staged v99 must not move current, nor advertise itself."""
    repo = _catalogue_repo(versions=(V1, V2, V99))
    assert repo.current_version(IDENTITY) == 2
    repo.install(user_id="alice", identity_id=IDENTITY, version=2)
    assert repo.update_available(user_id="alice", identity_id=IDENTITY) is False
    repo_v1 = _catalogue_repo(versions=(V1, V2, V99))
    repo_v1.install(user_id="alice", identity_id=IDENTITY, version=1)
    assert repo_v1.update_available(user_id="alice", identity_id=IDENTITY) is True


def test_publication_catalogue_has_no_rollback_current_mutator() -> None:
    assert not hasattr(PublicationCatalogue, "set_current_version")


def test_seed_current_establishes_first_pointer() -> None:
    catalogue = PublicationCatalogue()
    catalogue.seed(_fixture_publication(V1), current=True)
    assert catalogue.current_version(IDENTITY) == 1


def test_seed_current_advances_only_to_a_higher_version() -> None:
    catalogue = PublicationCatalogue()
    catalogue.seed(_fixture_publication(V1), current=True)
    catalogue.seed(_fixture_publication(V2), current=True)
    assert catalogue.current_version(IDENTITY) == 2


def test_seed_current_same_version_is_idempotent() -> None:
    catalogue = PublicationCatalogue()
    item = _fixture_publication(V1)
    catalogue.seed(item, current=True)
    catalogue.seed(item, current=True)
    assert catalogue.current_version(IDENTITY) == 1


def test_seed_existing_historical_current_cannot_roll_pointer_back() -> None:
    catalogue = PublicationCatalogue()
    catalogue.seed(_fixture_publication(V1), current=True)
    catalogue.seed(_fixture_publication(V2), current=True)
    with pytest.raises(ValueError, match="publication_current_version_regression"):
        catalogue.seed(_fixture_publication(V1), current=True)
    assert catalogue.current_version(IDENTITY) == 2


def test_seed_historical_current_false_never_changes_pointer() -> None:
    catalogue = PublicationCatalogue()
    catalogue.seed(_fixture_publication(V2), current=True)
    catalogue.seed(_fixture_publication(V1), current=False)
    assert catalogue.current_version(IDENTITY) == 2


def test_seed_does_not_repair_missing_current_pointer() -> None:
    catalogue = PublicationCatalogue()
    catalogue.seed(_fixture_publication(V1), current=True)
    del catalogue._current[IDENTITY]
    with pytest.raises(LookupError, match="publication_current_version_unbound"):
        catalogue.seed(_fixture_publication(V2), current=True)
    with pytest.raises(LookupError, match="publication_current_version_unbound"):
        catalogue.current_version(IDENTITY)


def test_seed_does_not_repair_corrupt_current_pointer() -> None:
    catalogue = PublicationCatalogue()
    catalogue.seed(_fixture_publication(V1), current=True)
    catalogue._current[IDENTITY] = 99
    with pytest.raises(LookupError, match="publication_current_version_invalid"):
        catalogue.seed(_fixture_publication(V2), current=True)
    with pytest.raises(LookupError, match="publication_current_version_invalid"):
        catalogue.current_version(IDENTITY)


def test_publish_historical_version_keeps_current_pointer() -> None:
    catalogue = PublicationCatalogue()
    catalogue.publish(_semantic_publication(2))
    catalogue.publish(_semantic_publication(1))
    assert catalogue.current_version(IDENTITY) == 2


def test_library_current_pointer_must_resolve_to_a_real_version() -> None:
    # An explicit pointer that names a version absent from the catalogue is
    # invalid; it is never silently replaced by a numeric maximum.
    repo = _catalogue_repo(current_versions={IDENTITY: 77})
    with pytest.raises(LookupError):
        repo.current_version(IDENTITY)
    unknown = _catalogue_repo()
    with pytest.raises(LookupError):
        unknown.current_version("unknown.identity")


def test_missing_current_pointer_fails_closed_for_update_projection() -> None:
    repo = _catalogue_repo()
    del repo.catalogue._current[IDENTITY]
    repo.install(user_id="alice", identity_id=IDENTITY, version=1)
    assert repo.update_available(user_id="alice", identity_id=IDENTITY) is False


def test_current_lower_than_installed_does_not_advertise_an_update() -> None:
    repo = _catalogue_repo(current_versions={IDENTITY: 1})
    repo.install(user_id="alice", identity_id=IDENTITY, version=2)
    assert repo.update_available(user_id="alice", identity_id=IDENTITY) is False


def test_withdrawn_current_is_not_an_available_update() -> None:
    repo = _catalogue_repo()
    repo.install(user_id="alice", identity_id=IDENTITY, version=1)
    repo.catalogue.withdraw(IDENTITY, 2)
    assert repo.update_available(user_id="alice", identity_id=IDENTITY) is False


def test_install_and_upgrade_reject_withdrawn_exact_versions() -> None:
    repo = _catalogue_repo()
    repo.catalogue.withdraw(IDENTITY, 2)
    with pytest.raises(LibraryIdentityNotFound) as install_error:
        repo.install(user_id="alice", identity_id=IDENTITY, version=2)
    assert str(install_error.value) == PUBLICATION_WITHDRAWN

    repo.install(user_id="alice", identity_id=IDENTITY, version=1)
    with pytest.raises(LibraryIdentityNotFound) as upgrade_error:
        repo.upgrade(user_id="alice", identity_id=IDENTITY, to_version=2)
    assert str(upgrade_error.value) == PUBLICATION_WITHDRAWN


def test_upgrade_is_strictly_upward() -> None:
    repo = _catalogue_repo()
    repo.install(user_id="alice", identity_id=IDENTITY, version=2)
    with pytest.raises(LibraryIdentityNotFound, match="upgrade_requires_newer_version"):
        repo.upgrade(user_id="alice", identity_id=IDENTITY, to_version=1)


def test_install_pins_the_exact_version_and_never_auto_upgrades() -> None:
    repo = _catalogue_repo()
    binding = repo.install(user_id="alice", identity_id=IDENTITY, version=1)
    assert (binding.identity_id, binding.version, binding.pinned) == (IDENTITY, 1, True)
    assert repo.update_available(user_id="alice", identity_id=IDENTITY) is True
    # still pinned at v1 without an explicit upgrade
    installed = repo.get_install(user_id="alice", identity_id=IDENTITY)
    assert installed is not None
    assert installed.version == 1


def test_update_available_is_computed_not_stored() -> None:
    repo = _catalogue_repo()
    repo.install(user_id="alice", identity_id=IDENTITY, version=2)
    assert repo.update_available(user_id="alice", identity_id=IDENTITY) is False


def test_star_is_identity_scoped_and_survives_an_upgrade() -> None:
    repo = _catalogue_repo()
    repo.install(user_id="alice", identity_id=IDENTITY, version=1)
    repo.star(user_id="alice", identity_id=IDENTITY)
    assert repo.star_count(identity_id=IDENTITY) == 1
    repo.star(user_id="alice", identity_id=IDENTITY)
    assert repo.star_count(identity_id=IDENTITY) == 1
    repo.upgrade(user_id="alice", identity_id=IDENTITY, to_version=2)
    upgraded = repo.get_install(user_id="alice", identity_id=IDENTITY)
    assert upgraded is not None
    assert upgraded.version == 2
    assert repo.is_starred(user_id="alice", identity_id=IDENTITY) is True
    assert repo.star_count(identity_id=IDENTITY) == 1


def test_star_is_reversible_and_per_user() -> None:
    repo = _catalogue_repo()
    repo.star(user_id="alice", identity_id=IDENTITY)
    repo.star(user_id="bob", identity_id=IDENTITY)
    assert repo.star_count(identity_id=IDENTITY) == 2
    repo.unstar(user_id="alice", identity_id=IDENTITY)
    assert repo.is_starred(user_id="alice", identity_id=IDENTITY) is False
    assert repo.is_starred(user_id="bob", identity_id=IDENTITY) is True
    assert repo.star_count(identity_id=IDENTITY) == 1


def test_certification_is_version_scoped_and_mutation_is_required() -> None:
    repo = _catalogue_repo()
    assert repo.certification_state(identity_id=IDENTITY, version=1) == "certified"
    assert repo.certification_state(identity_id=IDENTITY, version=2) == "uncertified"
    # Only the LOCAL-DEMO path can mutate certification, and it records the
    # acting administrator; production certification stays NOT_CONNECTED.
    provenance = repo.certify_local_demo(
        identity_id=IDENTITY, version=2, certified_by="local-cert-admin"
    )
    assert provenance == "local_demo_certification"
    assert repo.certification_state(identity_id=IDENTITY, version=2) == "certified"
    assert NOT_CONNECTED == "NOT_CONNECTED"

def test_unclosed_draft_cannot_confirm() -> None:
    """Prevents an immutable version that could never become SAVED."""
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    with pytest.raises(ValueError, match="confirm requires semantic closure"):
        service.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    service.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    saved = service.save(owner_user_id="alice", definition_id=draft.definition_id)
    assert saved.axes.confirmation == "CONFIRMED"
    assert saved.axes.retention == "SAVED"


def test_run_scoped_noncanonical_input_cannot_be_semantic_closed() -> None:
    """A run-scoped input must never become reusable authority."""
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    spec = CalculationSpec(
        calculation_id="calc.demo",
        expression=LiteralOperand(value=1),
        inputs=(CalculationInputSpec(role="r", provenance="ad_hoc_metric"),),
        unit="count",
    )
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=spec
    )
    with pytest.raises(ValueError, match="noncanonical"):
        service.mark_semantic_closed(
            owner_user_id="alice", definition_id=draft.definition_id
        )


def test_semantic_closure_requires_an_existing_governed_published_metric() -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    spec = _spec().model_copy(
        update={
            "inputs": (
                CalculationInputSpec(
                    role="r",
                    provenance="published_gold",
                    metric_key="missing.metric",
                ),
            )
        }
    )
    draft = service.create_draft(owner_user_id="alice", title="M", calculation=spec)
    with pytest.raises(ValueError, match="existing eligible published_gold metric"):
        service.mark_semantic_closed(
            owner_user_id="alice", definition_id=draft.definition_id
        )


@pytest.mark.parametrize(
    "provenance", ["definition_backed_computed", "product_published_state"]
)
def test_semantic_closure_rejects_unimplemented_formal_provenance(
    provenance: str,
) -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    spec = _spec().model_copy(
        update={
            "inputs": (
                CalculationInputSpec(
                    role="r", provenance=provenance, metric_key="demo.revenue"
                ),
            )
        }
    )
    draft = service.create_draft(owner_user_id="alice", title="M", calculation=spec)
    with pytest.raises(ValueError, match="provenance authority is unavailable"):
        service.mark_semantic_closed(
            owner_user_id="alice", definition_id=draft.definition_id
        )


def test_parameter_contract_mismatch_is_rejected() -> None:
    from src.nl2sql.artifacts.custom_definition import ParameterContract

    with pytest.raises(Exception):
        DefinitionVersion(
            definition_id="def_" + "a" * 32,
            version=1,
            calculation=_spec(),
            parameter_contract=ParameterContract(),
            title="t",
            created_at=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ),
        )


def test_material_edit_invalidates_semantic_closure() -> None:
    """A closure proof of OLD semantics must not cover NEW semantics."""
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    edited = service.update_draft(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        calculation=_spec(calculation_id="calc.demo2"),
    )
    assert edited.semantic_closed is False
    with pytest.raises(ValueError, match="confirm requires semantic closure"):
        service.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    service.confirm(owner_user_id="alice", definition_id=draft.definition_id)


def test_changed_parameter_surface_rederives_the_contract() -> None:
    """A new calculation must not silently keep the OLD parameter surface."""
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    assert [p.name for p in draft.parameter_contract.parameters] == ["threshold"]
    renamed = CalculationSpec(
        calculation_id="calc.demo",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key="demo.revenue"
            ),
        ),
        unit="count",
        parameters=(
            ParameterSpec(name="window", value_type="integer", required=True),
        ),
    )
    edited = service.update_draft(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        calculation=renamed,
    )
    assert [p.name for p in edited.parameter_contract.parameters] == ["window"]


def test_mismatched_explicit_contract_is_rejected_atomically() -> None:
    from src.nl2sql.artifacts.custom_definition import ParameterContract

    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    before = service.list_owned(owner_user_id="alice")[0].current_version
    with pytest.raises(Exception):
        service.update_draft(
            owner_user_id="alice",
            definition_id=draft.definition_id,
            calculation=CalculationSpec(
                calculation_id="calc.demo",
                expression=LiteralOperand(value=1),
                inputs=(
                    CalculationInputSpec(
                        role="r", provenance="published_gold", metric_key="demo.revenue"
                    ),
                ),
                unit="count",
                parameters=(
                    ParameterSpec(name="window", value_type="integer", required=True),
                ),
            ),
            parameter_contract=ParameterContract(parameters=tuple(_spec().parameters)),
        )
    after = service.list_owned(owner_user_id="alice")[0].current_version
    assert after.checksum == before.checksum
    assert [p.name for p in after.parameter_contract.parameters] == ["threshold"]


def test_title_only_update_preserves_closure_and_semantics() -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    before = service.list_owned(owner_user_id="alice")[0].current_version
    edited = service.update_draft(
        owner_user_id="alice", definition_id=draft.definition_id, title="Renamed"
    )
    assert edited.title == "Renamed"
    assert edited.semantic_closed is True
    assert edited.calculation.checksum == before.calculation.checksum
    assert edited.parameter_contract.checksum == before.parameter_contract.checksum


def test_update_after_confirm_is_rejected() -> None:
    service, definition_id, _exact = _closed_service()
    with pytest.raises(ValueError, match="confirmed definition requires a new version"):
        service.update_draft(
            owner_user_id="alice", definition_id=definition_id, title="X"
        )
