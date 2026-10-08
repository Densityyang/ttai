"""Control-PG publication catalogue: the DURABLE implementation of the port.

MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
metadata, the typed JSON artifacts, the hashes, the references and the
lifecycle.  This module is that layer for the PUBLICATION catalogue; the
process-local `PublicationCatalogue` remains the default for infra-dev and
for tests.

Every fail-closed rule of the port is preserved, and the database itself
enforces the same invariants
(docker/migrations/control/005_product_artifacts.sql):

* a published VERSION is IMMUTABLE, so certification and withdrawal are the
  only mutable axes and the reusable semantic package can never be rewritten;
* certification always names who certified it, so provenance cannot be
  dropped by a bare update;
* the current-version pointer is EXPLICIT and its composite foreign key makes
  it impossible to name a version that does not exist, so it is never
  inferred as max(version);
* the pointer is MONOTONIC, so publishing a historical version preserves the
  already established pointer instead of moving it backwards.

Two invariants cannot be expressed by the database and are therefore
re-verified on every read: a row must decode as the version it declares, and
its semantic package must satisfy the published-package invariant (the
parameter contract must equal the calculation parameters).  A row written by a
direct SQL writer that violates either one is REFUSED instead of being served.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.database import DatabasePurpose, create_runtime_async_engine
from src.core.settings import get_settings
from src.nl2sql.artifacts.custom_definition import ParameterContract
from src.nl2sql.artifacts.publication import (
    PublishedSemanticPackage,
    PublishedVersion,
)
from src.nl2sql.semantic.calculation_contract import CalculationSpec

# Every column one immutable version needs, in one place so no read path can
# silently drop the semantic package it is supposed to re-verify.
_SELECT_VERSION_COLUMNS = """
    identity_id, version, title, owner_user_id, owner_label, source_label,
    definition_checksum, published_at, unit, display_value,
    derived_from_identity, derived_from_version, semantic,
    certification, certified_by, withdrawn
"""

# A NEW version is INSERTed once and never rewritten: it starts UNCERTIFIED and
# not withdrawn, and only the two mutable axes are ever updated afterwards.
_INSERT_VERSION = """
    INSERT INTO product_publication_versions (
      identity_id, version, title, owner_user_id, owner_label, source_label,
      definition_checksum, published_at, unit, display_value,
      derived_from_identity, derived_from_version, semantic,
      certification, certified_by, withdrawn
    ) VALUES (
      :identity_id, :version, :title, :owner_user_id, :owner_label, :source_label,
      :definition_checksum, :published_at, :unit, :display_value,
      :derived_from_identity, :derived_from_version, CAST(:semantic AS jsonb),
      'uncertified', NULL, FALSE
    )
"""

# The EXPLICIT pointer row.  The version row must already exist: the composite
# foreign key is what makes "current" impossible to point at a missing version.
# An existing row is only ever ADVANCED, so a historical publication leaves it
# exactly where it was; its title is never rewritten either, matching the port's
# set-once title semantics.
_INSERT_OR_ADVANCE_POINTER = """
    INSERT INTO product_publication_identities (identity_id, title, current_version)
    VALUES (
      :identity_id,
      COALESCE(
        (SELECT version_row.title
           FROM product_publication_versions AS version_row
          WHERE version_row.identity_id = :identity_id
          ORDER BY version_row.created_at, version_row.version
          LIMIT 1),
        :title
      ),
      :current_version
    )
    ON CONFLICT (identity_id) DO UPDATE
       SET current_version = EXCLUDED.current_version
     WHERE product_publication_identities.current_version < EXCLUDED.current_version
"""


class PublicationIntegrityError(RuntimeError):
    """A stored publication row cannot be trusted as the version it declares.

    The database makes a published version immutable, but it cannot check the
    SEMANTIC package inside the JSONB column against the calculation it claims
    to describe.  A row that does not decode, or whose parameter contract does
    not match its calculation parameters, is reported as this one typed error
    instead of leaking a decoder traceback or being served as if it were valid.
    """

    def __init__(self) -> None:
        super().__init__("publication_semantic_package_incoherent")


def _semantic_payload(package: PublishedSemanticPackage) -> str:
    """The JSONB form of one reusable semantic package.

    The package is stored as its TWO typed parts plus its provenance, so a read
    re-validates each part independently and the published-package invariant is
    enforced again instead of being assumed.
    """

    return json.dumps(
        {
            "calculation": package.calculation.model_dump(mode="json"),
            "parameter_contract": package.parameter_contract.model_dump(mode="json"),
            "source_definition_id": package.source_definition_id,
            "source_definition_version": package.source_definition_version,
            "source_definition_checksum": package.source_definition_checksum,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _semantic_package(raw: Any) -> PublishedSemanticPackage | None:
    """Rebuild one package and RE-VERIFY its invariant before returning it."""

    if raw is None:
        return None
    try:
        payload: dict[str, Any] = (
            dict(raw) if isinstance(raw, Mapping) else json.loads(str(raw))
        )
        return PublishedSemanticPackage(
            calculation=CalculationSpec.model_validate(payload["calculation"]),
            # The stored contract is re-validated AS STORED, never re-derived
            # from the calculation: re-deriving it would silently REPAIR an
            # incoherent package instead of refusing it.
            #
            # `strict=False` is a JSON-SHAPE allowance only - a JSON array is
            # not a Python tuple and the parameter models are strict - and it
            # relaxes neither `extra="forbid"` nor the coherence invariant that
            # PublishedSemanticPackage enforces on construction.
            parameter_contract=ParameterContract.model_validate(
                payload["parameter_contract"], strict=False
            ),
            source_definition_id=str(payload["source_definition_id"]),
            source_definition_version=int(payload["source_definition_version"]),
            source_definition_checksum=str(payload["source_definition_checksum"]),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise PublicationIntegrityError() from exc


def _version(row: Mapping[Any, Any]) -> PublishedVersion:
    """Rebuild one immutable version from its stored row."""

    try:
        return PublishedVersion(
            identity_id=str(row["identity_id"]),
            version=int(row["version"]),
            title=str(row["title"]),
            owner_user_id=str(row["owner_user_id"]),
            owner_label=str(row["owner_label"]),
            source_label=str(row["source_label"]),
            definition_checksum=str(row["definition_checksum"]),
            published_at=str(row["published_at"]),
            unit=str(row["unit"]),
            value=(None if row["display_value"] is None else str(row["display_value"])),
            derived_from_identity=(
                None
                if row["derived_from_identity"] is None
                else str(row["derived_from_identity"])
            ),
            derived_from_version=(
                None
                if row["derived_from_version"] is None
                else int(row["derived_from_version"])
            ),
            semantic=_semantic_package(row["semantic"]),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise PublicationIntegrityError() from exc


def _is_duplicate_version(exc: IntegrityError) -> bool:
    """Whether one failed INSERT lost the (identity_id, version) key race."""

    constraint = getattr(exc.orig, "constraint_name", None)
    if constraint is not None:
        return str(constraint) == "product_publication_versions_pkey"
    return "product_publication_versions_pkey" in str(exc.orig)


class ControlPublicationCatalogue:
    """Durable, fail-closed publication catalogue on Control PostgreSQL."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if (database_url is None) == (engine is None):
            raise ValueError("provide exactly one of database_url or engine")
        if engine is not None:
            self._engine = engine
        else:
            self._engine = create_runtime_async_engine(
                database_url or "",
                purpose=DatabasePurpose.CONTROL_APP,
                application_name="ttai-product-publication",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        """Prove the publication catalogue is actually reachable, not merely
        configured, so startup readiness cannot claim a store it cannot reach.
        """

        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def get(self, identity_id: str, version: int) -> PublishedVersion | None:
        async with self._engine.connect() as connection:
            return await self._read_version(connection, identity_id, version)

    async def versions(self, identity_id: str) -> tuple[PublishedVersion, ...]:
        async with self._engine.connect() as connection:
            return await self._read_versions(connection, identity_id)

    async def identities(self) -> tuple[str, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT DISTINCT identity_id "
                            "FROM product_publication_versions "
                            "ORDER BY identity_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return tuple(str(row["identity_id"]) for row in rows)

    async def current_version(self, identity_id: str) -> int:
        """The EXPLICIT pointer; never max(version)."""

        async with self._engine.connect() as connection:
            return await self._current_version_in(connection, identity_id)

    async def title(self, identity_id: str) -> str:
        # The title is set ONCE by the first version written for an identity and
        # is never rewritten, so it is read from the earliest version row - also
        # when that identity has no pointer yet.
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT title FROM product_publication_versions "
                        "WHERE identity_id = :identity_id "
                        "ORDER BY created_at, version LIMIT 1"
                    ),
                    {"identity_id": identity_id},
                )
            ).scalar_one_or_none()
        return identity_id if row is None else str(row)

    async def certification_state(self, identity_id: str, version: int) -> str:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT certification FROM product_publication_versions "
                        "WHERE identity_id = :identity_id AND version = :version"
                    ),
                    {"identity_id": identity_id, "version": version},
                )
            ).scalar_one_or_none()
        return "uncertified" if row is None else str(row)

    async def certified_by(self, identity_id: str, version: int) -> str | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT certified_by FROM product_publication_versions "
                        "WHERE identity_id = :identity_id AND version = :version"
                    ),
                    {"identity_id": identity_id, "version": version},
                )
            ).scalar_one_or_none()
        return None if row is None else str(row)

    async def is_withdrawn(self, identity_id: str, version: int) -> bool:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT withdrawn FROM product_publication_versions "
                        "WHERE identity_id = :identity_id AND version = :version"
                    ),
                    {"identity_id": identity_id, "version": version},
                )
            ).scalar_one_or_none()
        return bool(row)

    async def seed(
        self, item: PublishedVersion, *, current: bool = False
    ) -> PublishedVersion:
        """Fixture/bootstrap ONLY, with fail-closed overwrite protection.

        Absent            -> seed allowed.
        Present, equal    -> idempotent (acceptable).
        Present, different-> fail closed; a fixture must never silently replace a
                             publication.  Product publication uses publish().
        """

        async with self._engine.begin() as connection:
            existing = await self._read_version(
                connection, item.identity_id, item.version
            )
            if existing is not None and existing != item:
                raise ValueError("seed refuses to overwrite an existing publication")

            # `seed` is allowed to establish or advance bootstrap state, but it
            # is still subject to the same explicit monotonic-current invariant as
            # product publication.  In particular, an existing identity with a
            # missing/corrupt pointer must not be repaired by guessing a maximum.
            pointer: int | None = None
            if current:
                existing_versions = await self._read_versions(
                    connection, item.identity_id
                )
                if not existing_versions:
                    # A pointer row cannot exist without the version it points at
                    # (composite foreign key), so this state is unreachable; it is
                    # refused rather than repaired if it is ever seen.
                    if (
                        await self._read_pointer(connection, item.identity_id)
                        is not None
                    ):
                        raise ValueError("publication_current_version_invalid")
                    pointer = item.version
                else:
                    current_version = await self._current_version_in(
                        connection, item.identity_id
                    )
                    if item.version < current_version:
                        raise ValueError("publication_current_version_regression")
                    if item.version > current_version:
                        pointer = item.version

            # The immutable version row is written FIRST: the pointer references
            # it, and the database refuses a pointer to a missing version.
            if existing is None:
                await self._insert_version(connection, item)
            if pointer is not None:
                await self._write_pointer(
                    connection, item.identity_id, item.title, pointer
                )
        return item

    async def publish(self, item: PublishedVersion) -> PublishedVersion:
        """Append ONE immutable semantic version and monotonically advance.

        Publishing a historical version preserves the already established
        current pointer.  Existing identities with a missing or corrupt pointer
        fail closed instead of being repaired by inference.
        """

        async with self._engine.begin() as connection:
            if (
                await self._read_version(connection, item.identity_id, item.version)
                is not None
            ):
                raise ValueError("published version is immutable")
            if item.semantic is None:
                raise ValueError("publication requires a semantic package")

            existing_versions = await self._read_versions(connection, item.identity_id)
            if existing_versions:
                current: int | None = await self._current_version_in(
                    connection, item.identity_id
                )
            else:
                current = None

            try:
                await self._insert_version(connection, item)
            except IntegrityError as exc:
                # A concurrent publisher of the SAME (identity, version) loses
                # the primary key race: that is still the immutability refusal.
                if not _is_duplicate_version(exc):
                    raise
                raise ValueError("published version is immutable") from exc

            # The first published version establishes current.  Later historical
            # publications never move that pointer backward.  A NEW version
            # starts UNCERTIFIED; historical certification is untouched.
            if current is None or item.version > current:
                await self._write_pointer(
                    connection, item.identity_id, item.title, item.version
                )
        return item

    async def certify_local_demo(
        self, identity_id: str, version: int, *, certified_by: str
    ) -> None:
        """LOCAL-DEMO certification; the semantic package is NOT touched."""

        async with self._engine.begin() as connection:
            if await self._read_version(connection, identity_id, version) is None:
                raise LookupError("publication_version_not_found")
            await connection.execute(
                text(
                    "UPDATE product_publication_versions "
                    "SET certification = 'certified', certified_by = :certified_by "
                    "WHERE identity_id = :identity_id AND version = :version"
                ),
                {
                    "identity_id": identity_id,
                    "version": version,
                    "certified_by": certified_by,
                },
            )

    async def withdraw(self, identity_id: str, version: int) -> None:
        """Source withdrawal; the semantic package is NOT touched."""

        async with self._engine.begin() as connection:
            if await self._read_version(connection, identity_id, version) is None:
                raise LookupError("publication_version_not_found")
            await connection.execute(
                text(
                    "UPDATE product_publication_versions SET withdrawn = TRUE "
                    "WHERE identity_id = :identity_id AND version = :version"
                ),
                {"identity_id": identity_id, "version": version},
            )

    # --- connection-scoped statements shared by the public methods ----------

    async def _read_version(
        self, connection: Any, identity_id: str, version: int
    ) -> PublishedVersion | None:
        row = (
            (
                await connection.execute(
                    text(
                        f"SELECT {_SELECT_VERSION_COLUMNS} "
                        "FROM product_publication_versions "
                        "WHERE identity_id = :identity_id AND version = :version"
                    ),
                    {"identity_id": identity_id, "version": version},
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _version(row)

    async def _read_versions(
        self, connection: Any, identity_id: str
    ) -> tuple[PublishedVersion, ...]:
        rows = (
            (
                await connection.execute(
                    text(
                        f"SELECT {_SELECT_VERSION_COLUMNS} "
                        "FROM product_publication_versions "
                        "WHERE identity_id = :identity_id ORDER BY version"
                    ),
                    {"identity_id": identity_id},
                )
            )
            .mappings()
            .all()
        )
        return tuple(_version(row) for row in rows)

    async def _read_pointer(self, connection: Any, identity_id: str) -> int | None:
        pointer = (
            await connection.execute(
                text(
                    "SELECT current_version FROM product_publication_identities "
                    "WHERE identity_id = :identity_id"
                ),
                {"identity_id": identity_id},
            )
        ).scalar_one_or_none()
        return None if pointer is None else int(pointer)

    async def _current_version_in(self, connection: Any, identity_id: str) -> int:
        """The EXPLICIT pointer for one identity; never max(version)."""

        if not await self._read_versions(connection, identity_id):
            raise LookupError("publication_identity_not_found")
        current = await self._read_pointer(connection, identity_id)
        if current is None:
            raise LookupError("publication_current_version_unbound")
        if await self._read_version(connection, identity_id, current) is None:
            raise LookupError("publication_current_version_invalid")
        return current

    async def _insert_version(self, connection: Any, item: PublishedVersion) -> None:
        await connection.execute(
            text(_INSERT_VERSION),
            {
                "identity_id": item.identity_id,
                "version": item.version,
                "title": item.title,
                "owner_user_id": item.owner_user_id,
                "owner_label": item.owner_label,
                "source_label": item.source_label,
                "definition_checksum": item.definition_checksum,
                "published_at": item.published_at,
                "unit": item.unit,
                "display_value": item.value,
                "derived_from_identity": item.derived_from_identity,
                "derived_from_version": item.derived_from_version,
                "semantic": (
                    None if item.semantic is None else _semantic_payload(item.semantic)
                ),
            },
        )

    async def _write_pointer(
        self,
        connection: Any,
        identity_id: str,
        title: str,
        current_version: int,
    ) -> None:
        await connection.execute(
            text(_INSERT_OR_ADVANCE_POINTER),
            {
                "identity_id": identity_id,
                "title": title,
                "current_version": current_version,
            },
        )


__all__ = [
    "ControlPublicationCatalogue",
    "PublicationIntegrityError",
]
