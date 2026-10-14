"""The definition-store PORT: durable reads/writes with NO lifecycle policy.

MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
metadata, the typed JSON artifacts, the hashes, the references and the
lifecycle.  Custom Definitions are exactly such metadata, so the definition
store is a swappable PORT rather than a process-local dict buried in the
service.

Division of authority (deliberate, and the whole point of this module):

* this PORT owns ONLY storage: the stable identity with its CURRENT axes and
  current immutable version, the exact immutable versions, and the per-version
  private lifecycle.  It performs NO policy - no version boundary, no closure
  gate, no axis implication, no lifecycle derivation, no publication
  projection.
* CustomDefinitionService owns ALL of that policy and is the only place it
  lives, so a semantic change is made in ONE place regardless of backend.

Two implementations satisfy this port:

* InMemoryDefinitionStore - the process-local default for infra-dev and tests,
  whose observable behaviour is bit-for-bit what the service did before the
  extraction;
* ControlDefinitionStore - the durable Control-PostgreSQL implementation, which
  additionally has the documented invariants enforced by the database itself.

Defining the port as a runtime Protocol (rather than an abstract base class)
keeps the two implementations independent: neither is a subclass of the other,
so a divergence is a type error at the call site instead of a silent override.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.nl2sql.artifacts.custom_definition import (
    CustomDefinition,
    DefinitionVersion,
    DefinitionVersionLifecycle,
)


@runtime_checkable
class DefinitionStore(Protocol):
    """Storage-only access to definitions, exact versions and lifecycle."""

    async def ping(self) -> None:
        """Prove the store is reachable; a process-local store is trivial."""
        ...

    async def close(self) -> None:
        """Release any resources the store owns (a no-op when it owns none)."""
        ...

    async def get_definition(self, *, definition_id: str) -> CustomDefinition | None:
        """The stable identity with its current axes and current version."""
        ...

    async def list_definitions(self) -> tuple[CustomDefinition, ...]:
        """Every definition the store holds, in stable insertion order."""
        ...

    async def put_definition(self, *, definition: CustomDefinition) -> None:
        """Create or update the identity/axes/current-version projection."""
        ...

    async def get_version(
        self, *, definition_id: str, version: int
    ) -> DefinitionVersion | None:
        """One EXACT immutable version, or None when it was never confirmed."""
        ...

    async def list_versions(
        self, *, definition_id: str
    ) -> tuple[DefinitionVersion, ...]:
        """Every exact immutable version of one definition, ascending."""
        ...

    async def put_version(self, *, version: DefinitionVersion) -> None:
        """Append one EXACT immutable version (never an in-place rewrite)."""
        ...

    async def get_lifecycle(
        self, *, definition_id: str, version: int
    ) -> DefinitionVersionLifecycle | None:
        """The recorded private lifecycle of one exact version, if recorded."""
        ...

    async def put_lifecycle(
        self,
        *,
        definition_id: str,
        version: int,
        lifecycle: DefinitionVersionLifecycle,
    ) -> None:
        """Record the private lifecycle of one exact version."""
        ...


class InMemoryDefinitionStore:
    """Process-local definition store: the non-durable default.

    It stores EXACTLY the state the service used to hold in private dicts, so
    the extraction changes no observable behaviour.  The mutable current version
    is carried by the CustomDefinition itself (there is deliberately no second
    draft map), which is what makes the port small enough to have no policy.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, CustomDefinition] = {}
        self._versions: dict[tuple[str, int], DefinitionVersion] = {}
        self._lifecycle: dict[tuple[str, int], DefinitionVersionLifecycle] = {}

    async def ping(self) -> None:
        """Readiness probe.  A process-local store is trivially reachable."""

    async def close(self) -> None:
        """Nothing to release."""

    async def get_definition(self, *, definition_id: str) -> CustomDefinition | None:
        return self._definitions.get(definition_id)

    async def list_definitions(self) -> tuple[CustomDefinition, ...]:
        return tuple(self._definitions.values())

    async def put_definition(self, *, definition: CustomDefinition) -> None:
        self._definitions[definition.definition_id] = definition

    async def get_version(
        self, *, definition_id: str, version: int
    ) -> DefinitionVersion | None:
        return self._versions.get((definition_id, version))

    async def list_versions(
        self, *, definition_id: str
    ) -> tuple[DefinitionVersion, ...]:
        return tuple(
            exact
            for (candidate_id, _version), exact in self._versions.items()
            if candidate_id == definition_id
        )

    async def put_version(self, *, version: DefinitionVersion) -> None:
        self._versions[(version.definition_id, version.version)] = version

    async def get_lifecycle(
        self, *, definition_id: str, version: int
    ) -> DefinitionVersionLifecycle | None:
        return self._lifecycle.get((definition_id, version))

    async def put_lifecycle(
        self,
        *,
        definition_id: str,
        version: int,
        lifecycle: DefinitionVersionLifecycle,
    ) -> None:
        self._lifecycle[(definition_id, version)] = lifecycle


__all__ = [
    "DefinitionStore",
    "InMemoryDefinitionStore",
]
