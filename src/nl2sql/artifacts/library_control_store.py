"""Control-PG personal library repository: the DURABLE implementation of the port.

MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
metadata, the typed JSON artifacts, the hashes, the references and the
lifecycle.  This module is that layer for the PERSONAL library state; the
process-local repository remains the default for infra-dev and for tests.

Division of authority (no duplication):

* the COMPOSED PublicationCatalogue owns SOURCE/PUBLISHED state: versions, the
  explicit current pointer, the immutable semantic package, certification and
  withdrawal.  Every catalogue read below is DELEGATED to it, so this store
  never grows a second current-version map.
* this repository owns USER/PERSONAL state only: installs, Stars and withdrawal
  acknowledgement, in the three frozen tables of
  docker/migrations/control/005_product_artifacts.sql.

The failure modes are the SAME types carrying the SAME literal codes as
InMemoryLibraryRepository, because the port's callers catch them by type:
CertificationUnavailable/NOT_CONNECTED, LibraryIdentityNotFound with
"library_version_not_found" (default), "library_identity_not_found",
"library_install_required", "library_acknowledgement_version_mismatch",
PUBLICATION_WITHDRAWN and UPGRADE_REQUIRES_NEWER_VERSION.

Every personal mutation is ONE atomic statement (INSERT ... ON CONFLICT or
DELETE), so two concurrent callers cannot interleave a read and a write into a
state the in-memory port could not produce.  The personal tables additionally
carry foreign keys onto product_publication_versions, so the database itself
refuses an install or an acknowledgement of a version that does not exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.database import DatabasePurpose, create_runtime_async_engine
from src.core.settings import get_settings
from src.nl2sql.artifacts.library import (
    UPGRADE_REQUIRES_NEWER_VERSION,
    CertificationUnavailable,
    InstalledMetricBinding,
    LibraryIdentityNotFound,
)
from src.nl2sql.artifacts.publication import (
    LOCAL_DEMO_CERTIFICATION_PROVENANCE,
    NOT_CONNECTED,
    PUBLICATION_WITHDRAWN,
)

if TYPE_CHECKING:
    from src.nl2sql.artifacts.ports import CataloguePort

# Every column an install projection needs, in one place so no read path can
# silently drop the pin state.
_INSTALL_COLUMNS = "user_id, identity_id, version, pinned"

# Identity ordering is pinned to the C collation so a listing is byte-ordered
# exactly like the in-memory repository's sorted(...) (Python compares code
# points; a locale collation would not).
_SELECT_INSTALLS_OF = f"""
    SELECT {_INSTALL_COLUMNS}
      FROM product_library_installs
     WHERE user_id = :user_id
     ORDER BY identity_id COLLATE "C"
"""

# ONE guarded statement: the acknowledgement is written only when an install of
# THAT EXACT version exists, so the precondition cannot race an upgrade.  The
# no-op DO UPDATE keeps RETURNING populated for an idempotent re-acknowledgement.
_ACKNOWLEDGE_STATEMENT = """
    INSERT INTO product_library_withdrawal_acks (user_id, identity_id, version)
    SELECT :user_id, :identity_id, :version
     WHERE EXISTS (
       SELECT 1
         FROM product_library_installs
        WHERE user_id = :user_id
          AND identity_id = :identity_id
          AND version = :version
     )
    ON CONFLICT (user_id, identity_id, version)
    DO UPDATE SET version = product_library_withdrawal_acks.version
    RETURNING version
"""


def _binding_from_row(row: Mapping[Any, Any]) -> InstalledMetricBinding:
    """Rebuild one install binding from its durable row."""

    return InstalledMetricBinding(
        identity_id=str(row["identity_id"]),
        version=int(row["version"]),
        pinned=bool(row["pinned"]),
    )


class ControlLibraryRepository:
    """Durable personal library over a SHARED PublicationCatalogue."""

    def __init__(
        self,
        catalogue: CataloguePort,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if (database_url is None) == (engine is None):
            raise ValueError("provide exactly one of database_url or engine")
        self._catalogue = catalogue
        if engine is not None:
            self._engine = engine
        else:
            self._engine = create_runtime_async_engine(
                database_url or "",
                purpose=DatabasePurpose.CONTROL_APP,
                application_name="ttai-product-library",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        """Prove the personal library store is actually reachable, not merely
        configured, so startup readiness cannot claim a store it cannot reach.
        """

        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    # --- catalogue reads (delegated; NO second current-version map) -------
    async def current_version(self, identity_id: str) -> int:
        try:
            return await self._catalogue.current_version(identity_id)
        except LookupError as exc:
            raise LibraryIdentityNotFound(str(exc)) from exc

    async def published_versions(self, identity_id: str) -> tuple[int, ...]:
        published = await self._catalogue.versions(identity_id)
        return tuple(item.version for item in published)

    async def certification_state(self, *, identity_id: str, version: int) -> str:
        return await self._catalogue.certification_state(identity_id, version)

    async def is_withdrawn(self, *, identity_id: str, version: int) -> bool:
        return await self._catalogue.is_withdrawn(identity_id, version)

    async def forkable(self, *, identity_id: str, version: int) -> bool:
        """Forkable iff an exact published version carries a semantic package."""

        published = await self._catalogue.get(identity_id, version)
        return published is not None and published.forkable

    # --- install ----------------------------------------------------------
    async def install(
        self, *, user_id: str, identity_id: str, version: int
    ) -> InstalledMetricBinding:
        published = await self._catalogue.get(identity_id, version)
        if published is None:
            raise LibraryIdentityNotFound()
        if await self._catalogue.is_withdrawn(identity_id, version):
            raise LibraryIdentityNotFound(PUBLICATION_WITHDRAWN)
        binding = InstalledMetricBinding(identity_id=identity_id, version=version)
        # ONE atomic upsert: the (user, identity) install is re-pinned to the
        # requested version, exactly like the in-memory dict assignment.
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO product_library_installs (
                      user_id, identity_id, version, pinned, created_at, updated_at
                    ) VALUES (
                      :user_id, :identity_id, :version, TRUE, now(), now()
                    )
                    ON CONFLICT (user_id, identity_id) DO UPDATE
                       SET version = EXCLUDED.version,
                           pinned = EXCLUDED.pinned,
                           updated_at = now()
                    """
                ),
                {
                    "user_id": user_id,
                    "identity_id": identity_id,
                    "version": version,
                },
            )
        return binding

    async def uninstall(self, *, user_id: str, identity_id: str) -> None:
        """Idempotent: removing an absent install is acceptable."""

        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM product_library_installs "
                    "WHERE user_id = :user_id AND identity_id = :identity_id"
                ),
                {"user_id": user_id, "identity_id": identity_id},
            )

    async def _read_install(
        self, *, user_id: str, identity_id: str
    ) -> InstalledMetricBinding | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_INSTALL_COLUMNS} FROM product_library_installs "
                            "WHERE user_id = :user_id "
                            "AND identity_id = :identity_id"
                        ),
                        {"user_id": user_id, "identity_id": identity_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _binding_from_row(row)

    async def get_install(
        self, *, user_id: str, identity_id: str
    ) -> InstalledMetricBinding | None:
        return await self._read_install(user_id=user_id, identity_id=identity_id)

    async def installs_of(
        self, *, user_id: str
    ) -> tuple[InstalledMetricBinding, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(_SELECT_INSTALLS_OF), {"user_id": user_id}
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_binding_from_row(row) for row in rows)

    async def update_available(self, *, user_id: str, identity_id: str) -> bool:
        """Computed against the EXPLICIT pointer; never a numeric max."""

        binding = await self._read_install(user_id=user_id, identity_id=identity_id)
        if binding is None:
            return False
        try:
            current = await self._catalogue.current_version(identity_id)
        except LookupError:
            return False
        current_publication = await self._catalogue.get(identity_id, current)
        if current_publication is None or await self._catalogue.is_withdrawn(
            identity_id, current
        ):
            return False
        return current > binding.version

    async def upgrade(
        self, *, user_id: str, identity_id: str, to_version: int
    ) -> InstalledMetricBinding:
        """EXPLICIT only; never a silent upgrade."""

        binding = await self.get_install(user_id=user_id, identity_id=identity_id)
        if binding is None:
            raise LibraryIdentityNotFound("library_install_required")
        published = await self._catalogue.get(identity_id, to_version)
        if published is None:
            raise LibraryIdentityNotFound()
        if await self._catalogue.is_withdrawn(identity_id, to_version):
            raise LibraryIdentityNotFound(PUBLICATION_WITHDRAWN)
        if to_version <= binding.version:
            raise LibraryIdentityNotFound(UPGRADE_REQUIRES_NEWER_VERSION)
        return await self.install(
            user_id=user_id, identity_id=identity_id, version=to_version
        )

    # --- star (IDENTITY scoped) ------------------------------------------
    async def star(self, *, user_id: str, identity_id: str) -> int:
        """Star an EXISTING catalogue identity; never invent one."""

        existing = await self._catalogue.versions(identity_id)
        if not existing:
            raise LibraryIdentityNotFound("library_identity_not_found")
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO product_library_stars (user_id, identity_id)
                    VALUES (:user_id, :identity_id)
                    ON CONFLICT (user_id, identity_id) DO NOTHING
                    """
                ),
                {"user_id": user_id, "identity_id": identity_id},
            )
        return await self.star_count(identity_id=identity_id)

    async def unstar(self, *, user_id: str, identity_id: str) -> int:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM product_library_stars "
                    "WHERE user_id = :user_id AND identity_id = :identity_id"
                ),
                {"user_id": user_id, "identity_id": identity_id},
            )
        return await self.star_count(identity_id=identity_id)

    async def star_count(self, *, identity_id: str) -> int:
        async with self._engine.connect() as connection:
            count = (
                await connection.execute(
                    text(
                        "SELECT count(*) AS star_count FROM product_library_stars "
                        "WHERE identity_id = :identity_id"
                    ),
                    {"identity_id": identity_id},
                )
            ).scalar_one()
        return int(count)

    async def starred_of(self, *, user_id: str) -> tuple[str, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT identity_id FROM product_library_stars "
                            "WHERE user_id = :user_id "
                            'ORDER BY identity_id COLLATE "C"'
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(str(row["identity_id"]) for row in rows)

    async def is_starred(self, *, user_id: str, identity_id: str) -> bool:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT 1 AS starred FROM product_library_stars "
                            "WHERE user_id = :user_id "
                            "AND identity_id = :identity_id"
                        ),
                        {"user_id": user_id, "identity_id": identity_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return row is not None

    # --- withdrawal acknowledgement (personal only) ----------------------
    async def acknowledge_withdrawal(
        self, *, user_id: str, identity_id: str, version: int
    ) -> None:
        """Notification dismissal ONLY; never clears catalogue withdrawal.

        Requires an install of THAT EXACT version, so a user cannot
        acknowledge a withdrawal for a version they never received.
        """

        async with self._engine.begin() as connection:
            acknowledged = (
                (
                    await connection.execute(
                        text(_ACKNOWLEDGE_STATEMENT),
                        {
                            "user_id": user_id,
                            "identity_id": identity_id,
                            "version": version,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if acknowledged is not None:
                return
            # Distinguish "no install at all" from "installed at another
            # version" only AFTER the guarded statement proved it wrote nothing.
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT version FROM product_library_installs "
                            "WHERE user_id = :user_id "
                            "AND identity_id = :identity_id"
                        ),
                        {"user_id": user_id, "identity_id": identity_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LibraryIdentityNotFound("library_install_required")
        raise LibraryIdentityNotFound("library_acknowledgement_version_mismatch")

    async def is_acknowledged(
        self, *, user_id: str, identity_id: str, version: int
    ) -> bool:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT 1 AS acknowledged "
                            "FROM product_library_withdrawal_acks "
                            "WHERE user_id = :user_id "
                            "AND identity_id = :identity_id "
                            "AND version = :version"
                        ),
                        {
                            "user_id": user_id,
                            "identity_id": identity_id,
                            "version": version,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return row is not None

    # --- local-demo lifecycle (authority checked by the SERVICE) ---------
    async def certify_local_demo(
        self, *, identity_id: str, version: int, certified_by: str
    ) -> str:
        await self._catalogue.certify_local_demo(
            identity_id, version, certified_by=certified_by
        )
        return LOCAL_DEMO_CERTIFICATION_PROVENANCE

    async def withdraw(self, *, identity_id: str, version: int) -> None:
        await self._catalogue.withdraw(identity_id, version)


__all__ = [
    "CertificationUnavailable",
    "ControlLibraryRepository",
    "InstalledMetricBinding",
    "LibraryIdentityNotFound",
    "LOCAL_DEMO_CERTIFICATION_PROVENANCE",
    "NOT_CONNECTED",
    "PUBLICATION_WITHDRAWN",
    "UPGRADE_REQUIRES_NEWER_VERSION",
]
