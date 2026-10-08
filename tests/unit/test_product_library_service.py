"""Service-level vertical: shared catalogue, certification, withdrawal, fork."""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from src.nl2sql.artifacts.custom_definition import DefinitionVersionLifecycle
from src.nl2sql.artifacts.library import (
    PUBLICATION_WITHDRAWN,
    InMemoryLibraryRepository,
    LibraryIdentityNotFound,
)
from src.nl2sql.artifacts.product_library_service import (
    CertificationAuthority,
    CertificationForbidden,
    ProductLibraryService,
    PublicationNotForkable,
    WithdrawalForbidden,
)
from src.nl2sql.artifacts.publication import PublicationCatalogue
from src.nl2sql.artifacts.publication_service import PublicationService
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
    ParameterSpec,
)

ALICE = "alice"
BOB = "bob"
ADMIN = "local-cert-admin"


def _spec(calculation_id: str = "calc.rate") -> CalculationSpec:
    return CalculationSpec(
        calculation_id=calculation_id,
        expression=LiteralOperand(value=Decimal("1")),
        inputs=(
            CalculationInputSpec(
                role="numerator",
                provenance="published_gold",
                metric_key="repair_service_archive_rate_overall_day",
            ),
        ),
        unit="ratio",
        parameters=(
            ParameterSpec(
                name="threshold",
                value_type="integer",
                required=True,
                allowed_values=("1", "2"),
            ),
        ),
    )


def _stack(
    *,
    activation: str = "local_real_data_demo",
    service_mode: str = "infra-dev",
) -> tuple[CustomDefinitionService, PublicationService, ProductLibraryService]:
    """One SHARED catalogue across all three services, exactly as the container builds it."""

    catalogue = PublicationCatalogue()
    definitions = CustomDefinitionService(
        governed_metric_keys={"repair_service_archive_rate_overall_day"}
    )
    publications = PublicationService(definitions=definitions, catalogue=catalogue)
    library = InMemoryLibraryRepository(catalogue=catalogue)
    product = ProductLibraryService(
        catalogue=catalogue,
        library=library,
        definitions=definitions,
        publications=publications,
        certification_authority=CertificationAuthority(
            service_mode=service_mode,
            typed_runtime_activation=activation,
            admin_user_id=ADMIN,
        ),
    )
    return definitions, publications, product


def _published_v1(
    definitions: CustomDefinitionService, publications: PublicationService
) -> str:
    draft = definitions.create_draft(
        owner_user_id=ALICE, title="Rate", calculation=_spec()
    )
    definitions.mark_semantic_closed(
        owner_user_id=ALICE, definition_id=draft.definition_id
    )
    definitions.confirm(owner_user_id=ALICE, definition_id=draft.definition_id)
    definitions.save(owner_user_id=ALICE, definition_id=draft.definition_id)
    publications.publish(
        owner_user_id=ALICE, definition_id=draft.definition_id, version=1
    )
    return draft.definition_id


def _publish_revision(
    definitions: CustomDefinitionService,
    publications: PublicationService,
    definition_id: str,
    version: int,
) -> None:
    definitions.create_revision(owner_user_id=ALICE, definition_id=definition_id)
    definitions.mark_semantic_closed(
        owner_user_id=ALICE, definition_id=definition_id
    )
    definitions.confirm(owner_user_id=ALICE, definition_id=definition_id)
    definitions.save(owner_user_id=ALICE, definition_id=definition_id)
    publications.publish(
        owner_user_id=ALICE, definition_id=definition_id, version=version
    )


# --- lifecycle history --------------------------------------------------------


def test_exact_version_lifecycle_survives_a_later_revision() -> None:
    definitions, _publications, _product = _stack()
    draft = definitions.create_draft(
        owner_user_id=ALICE, title="Rate", calculation=_spec()
    )
    definition_id = draft.definition_id
    assert definitions.get_version_lifecycle(
        owner_user_id=ALICE, definition_id=definition_id, version=1
    ) == DefinitionVersionLifecycle()

    definitions.mark_semantic_closed(
        owner_user_id=ALICE, definition_id=definition_id
    )
    definitions.confirm(owner_user_id=ALICE, definition_id=definition_id)
    assert definitions.get_version_lifecycle(
        owner_user_id=ALICE, definition_id=definition_id, version=1
    ).confirmation == "CONFIRMED"
    assert definitions.get_version_lifecycle(
        owner_user_id=ALICE, definition_id=definition_id, version=1
    ).retention == "SESSION"

    definitions.save(owner_user_id=ALICE, definition_id=definition_id)
    definitions.create_revision(
        owner_user_id=ALICE, definition_id=definition_id
    )
    # v1's record is UNCHANGED by the revision; v2 starts fresh.
    v1 = definitions.get_version_lifecycle(
        owner_user_id=ALICE, definition_id=definition_id, version=1
    )
    assert (v1.confirmation, v1.retention) == ("CONFIRMED", "SAVED")
    v2 = definitions.get_version_lifecycle(
        owner_user_id=ALICE, definition_id=definition_id, version=2
    )
    assert (v2.confirmation, v2.retention) == ("DRAFT", "SESSION")


def test_public_owner_scoped_accessor_matches_absent_for_a_foreign_caller() -> None:
    definitions, _publications, _product = _stack()
    draft = definitions.create_draft(
        owner_user_id=ALICE, title="Rate", calculation=_spec()
    )
    from src.nl2sql.artifacts.service import DefinitionNotFound

    with pytest.raises(DefinitionNotFound):
        definitions.get_owned_definition(
            owner_user_id=BOB, definition_id=draft.definition_id
        )
    with pytest.raises(DefinitionNotFound):
        definitions.get_owned_definition(
            owner_user_id=BOB, definition_id="def_" + "0" * 32
        )
    with pytest.raises(DefinitionNotFound):
        definitions.get_version_lifecycle(
            owner_user_id=BOB, definition_id=draft.definition_id, version=1
        )


def test_historical_exact_version_is_publishable_after_a_revision() -> None:
    definitions, publications, _product = _stack()
    draft = definitions.create_draft(
        owner_user_id=ALICE, title="Rate", calculation=_spec()
    )
    definition_id = draft.definition_id
    definitions.mark_semantic_closed(
        owner_user_id=ALICE, definition_id=definition_id
    )
    definitions.confirm(owner_user_id=ALICE, definition_id=definition_id)
    definitions.save(owner_user_id=ALICE, definition_id=definition_id)
    # v2 exists BEFORE v1 is ever published
    definitions.create_revision(
        owner_user_id=ALICE, definition_id=definition_id
    )
    published = publications.publish(
        owner_user_id=ALICE, definition_id=definition_id, version=1
    )
    assert (published.identity_id, published.version) == (definition_id, 1)
    # ...and the CURRENT v2 axes were NOT projected
    current = definitions.get_owned_definition(
        owner_user_id=ALICE, definition_id=definition_id
    )
    assert current.current_version.version == 2
    assert current.axes.publication == "UNPUBLISHED"
    assert current.axes.confirmation == "DRAFT"
    assert current.axes.retention == "SESSION"


def test_republishing_an_exact_version_is_an_immutable_conflict() -> None:
    from src.nl2sql.artifacts.publication_service import PublicationConflict

    definitions, publications, _product = _stack()
    definition_id = _published_v1(definitions, publications)
    with pytest.raises(PublicationConflict):
        publications.publish(
            owner_user_id=ALICE, definition_id=definition_id, version=1
        )


def test_publishing_a_historical_version_never_moves_current_backward() -> None:
    definitions, publications, product = _stack()
    draft = definitions.create_draft(
        owner_user_id=ALICE, title="Rate", calculation=_spec()
    )
    definition_id = draft.definition_id
    definitions.mark_semantic_closed(
        owner_user_id=ALICE, definition_id=definition_id
    )
    definitions.confirm(owner_user_id=ALICE, definition_id=definition_id)
    definitions.save(owner_user_id=ALICE, definition_id=definition_id)
    _publish_revision(definitions, publications, definition_id, 2)
    publications.publish(owner_user_id=ALICE, definition_id=definition_id, version=1)

    assert product._catalogue.current_version(definition_id) == 2


def test_withdrawn_current_is_hidden_without_falling_back_to_history() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    _publish_revision(definitions, publications, definition_id, 2)
    product.withdraw(user_id=ALICE, identity_id=definition_id, version=2)

    assert product.catalogue_entries() == ()


def test_product_install_and_upgrade_use_the_withdrawn_conflict_code() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    _publish_revision(definitions, publications, definition_id, 2)
    product.withdraw(user_id=ALICE, identity_id=definition_id, version=2)

    with pytest.raises(LibraryIdentityNotFound) as install_error:
        product.install(user_id=BOB, identity_id=definition_id, version=2)
    assert str(install_error.value) == PUBLICATION_WITHDRAWN

    product.install(user_id=BOB, identity_id=definition_id, version=1)
    with pytest.raises(LibraryIdentityNotFound) as upgrade_error:
        product.upgrade(user_id=BOB, identity_id=definition_id, to_version=2)
    assert str(upgrade_error.value) == PUBLICATION_WITHDRAWN


def test_withdrawn_current_install_survives_and_ack_does_not_clear_withdrawal() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    product.install(user_id=BOB, identity_id=definition_id, version=1)
    product.withdraw(user_id=ALICE, identity_id=definition_id, version=1)

    entry = product.library_entries(user_id=BOB)[0]
    assert entry["installed_version"] == 1
    assert entry["current_version"] == 1
    assert entry["update_available"] is False
    assert entry["withdrawn"] is True
    result = product.acknowledge_withdrawal(
        user_id=BOB, identity_id=definition_id, version=1
    )
    assert result.withdrawn is True


def test_publish_after_withdrawal_restores_current_discovery() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    _publish_revision(definitions, publications, definition_id, 2)
    product.withdraw(user_id=ALICE, identity_id=definition_id, version=2)
    assert product.catalogue_entries() == ()

    _publish_revision(definitions, publications, definition_id, 3)
    entries = product.catalogue_entries()
    assert entries[0]["current_version"] == 3
    assert entries[0]["identity_id"] == definition_id


def test_invalid_current_pointer_is_omitted_from_product_projections() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    product.install(user_id=BOB, identity_id=definition_id, version=1)
    del product._catalogue._current[definition_id]

    assert product.catalogue_entries() == ()
    assert product.library_entries(user_id=BOB) == ()


# --- certification ------------------------------------------------------------


def test_certification_requires_the_configured_admin_only() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    for caller in (ALICE, BOB, "demo-analyst"):
        with pytest.raises(CertificationForbidden):
            product.certify(
                user_id=caller, identity_id=definition_id, version=1
            )
    result = product.certify(
        user_id=ADMIN, identity_id=definition_id, version=1
    )
    assert result.certification_state == "certified"
    assert result.authority_provenance == "local_demo_certification"
    assert result.production_certification == "NOT_CONNECTED"


def test_certification_authority_fails_closed_outside_local_real_demo() -> None:
    for activation, mode in (
        ("disabled", "infra-dev"),
        ("trusted_backend_authorization", "infra-dev"),
        ("local_real_data_demo", "product"),
    ):
        definitions, publications, product = _stack(
            activation=activation, service_mode=mode
        )
        definition_id = _published_v1(definitions, publications)
        with pytest.raises(CertificationForbidden):
            product.certify(
                user_id=ADMIN, identity_id=definition_id, version=1
            )


def test_certifying_a_historical_version_leaves_current_axes_alone() -> None:
    definitions, publications, product = _stack()
    draft = definitions.create_draft(
        owner_user_id=ALICE, title="Rate", calculation=_spec()
    )
    definition_id = draft.definition_id
    for action in (
        lambda: definitions.mark_semantic_closed(
            owner_user_id=ALICE, definition_id=definition_id
        ),
        lambda: definitions.confirm(
            owner_user_id=ALICE, definition_id=definition_id
        ),
        lambda: definitions.save(
            owner_user_id=ALICE, definition_id=definition_id
        ),
        lambda: publications.publish(
            owner_user_id=ALICE, definition_id=definition_id, version=1
        ),
    ):
        action()
    definitions.create_revision(
        owner_user_id=ALICE, definition_id=definition_id
    )
    product.certify(user_id=ADMIN, identity_id=definition_id, version=1)
    current = definitions.get_owned_definition(
        owner_user_id=ALICE, definition_id=definition_id
    )
    assert current.current_version.version == 2
    assert current.axes.certification == "UNCERTIFIED"
    versions = cast(
        "tuple[dict[str, object], ...]",
        product.catalogue_entries()[0]["versions"],
    )
    assert versions[0]["certification_state"] == "certified"


# --- withdrawal ----------------------------------------------------------------


def test_withdrawal_is_owner_only_and_never_evicts_installers() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    product.install(user_id=BOB, identity_id=definition_id, version=1)
    product.star(user_id=BOB, identity_id=definition_id)
    with pytest.raises(WithdrawalForbidden):
        product.withdraw(user_id=BOB, identity_id=definition_id, version=1)
    product.withdraw(user_id=ALICE, identity_id=definition_id, version=1)
    entries = product.library_entries(user_id=BOB)
    assert entries[0]["installed_version"] == 1, "no forced uninstall"
    assert entries[0]["withdrawn"] is True
    # the semantic package survives withdrawal
    published = product._catalogue.get(definition_id, 1)
    assert published is not None and published.semantic is not None


def test_acknowledgement_requires_the_installed_exact_version() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    from src.nl2sql.artifacts.product_library_service import LibraryVersionNotFound

    with pytest.raises(LibraryVersionNotFound):
        product.acknowledge_withdrawal(
            user_id=ALICE, identity_id=definition_id, version=1
        )
    product.install(user_id=ALICE, identity_id=definition_id, version=1)
    product.withdraw(user_id=ALICE, identity_id=definition_id, version=1)
    result = product.acknowledge_withdrawal(
        user_id=ALICE, identity_id=definition_id, version=1
    )
    assert result.withdrawal_acknowledged is True
    assert result.withdrawn is True, "acknowledgement never clears it"


# --- fork ----------------------------------------------------------------------


def test_fork_creates_a_fresh_private_definition_with_lineage() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    product.install(user_id=ALICE, identity_id=definition_id, version=1)
    fork = product.fork(
        user_id=ALICE, identity_id=definition_id, version=1, title="Fork"
    )
    assert fork.definition_id != definition_id
    assert fork.version == 1
    assert fork.semantic_closed is False
    assert fork.derived_from_definition_id == definition_id
    assert fork.derived_from_version == 1
    fork_definition = definitions.get_owned_definition(
        owner_user_id=ALICE, definition_id=fork.definition_id
    )
    assert fork_definition.axes.confirmation == "DRAFT"
    assert fork_definition.axes.retention == "SESSION"
    assert fork_definition.axes.publication == "UNPUBLISHED"
    assert fork_definition.axes.certification == "UNCERTIFIED"
    # no inherited Star for the NEW identifier
    product.unstar(user_id=ALICE, identity_id=fork.definition_id)
    assert product._library.star_count(identity_id=fork.definition_id) == 0


def test_fork_requires_an_install_of_the_exact_version() -> None:
    from src.nl2sql.artifacts.product_library_service import LibraryVersionNotFound

    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    with pytest.raises(LibraryVersionNotFound):
        product.fork(
            user_id=ALICE, identity_id=definition_id, version=1, title="F"
        )
    product.install(user_id=ALICE, identity_id=definition_id, version=1)
    product.fork(user_id=ALICE, identity_id=definition_id, version=1, title="F")


def test_a_seeded_legacy_publication_is_not_forkable() -> None:
    from src.nl2sql.artifacts.library import seed_catalogue_from_fixtures

    catalogue = PublicationCatalogue()
    seed_catalogue_from_fixtures(catalogue)
    definitions = CustomDefinitionService(
        governed_metric_keys={"repair_service_archive_rate_overall_day"}
    )
    publications = PublicationService(definitions=definitions, catalogue=catalogue)
    library = InMemoryLibraryRepository(catalogue=catalogue)
    product = ProductLibraryService(
        catalogue=catalogue,
        library=library,
        definitions=definitions,
        publications=publications,
        certification_authority=CertificationAuthority(
            service_mode="infra-dev",
            typed_runtime_activation="local_real_data_demo",
            admin_user_id=ADMIN,
        ),
    )
    identity = "demo.metric.margin"
    product.install(user_id=ALICE, identity_id=identity, version=1)
    with pytest.raises(PublicationNotForkable):
        product.fork(user_id=ALICE, identity_id=identity, version=1, title="F")


def test_forked_semantics_come_from_the_published_package() -> None:
    definitions, publications, product = _stack()
    definition_id = _published_v1(definitions, publications)
    product.install(user_id=ALICE, identity_id=definition_id, version=1)
    fork = product.fork(
        user_id=ALICE, identity_id=definition_id, version=1, title="Fork"
    )
    source = definitions.get_exact_version(
        owner_user_id=ALICE, definition_id=definition_id, version=1
    )
    assert fork.calculation.checksum == source.calculation.checksum
