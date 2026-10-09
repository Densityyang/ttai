"""The swappable product-store ports.

The Control-PG implementations are deliberately NOT subclasses of the
process-local ones, so the shared type of each port is the UNION of its two
implementations.  Naming that union once keeps every consumer honest: the
container, the services and the seed helper accept EITHER backend, and a
divergence between the two becomes a type error instead of a silent behaviour
difference.

Defined under TYPE_CHECKING only: every consumer imports it under the same
guard and annotates with it, so this module never joins a runtime import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.nl2sql.artifacts.confirmation_control_store import (
        ControlConfirmationAuditStore,
        ControlExplorationConfirmationStore,
    )
    from src.nl2sql.artifacts.definition_confirmation_audit import (
        InMemoryConfirmationAuditStore,
    )
    from src.nl2sql.artifacts.definition_control_store import ControlDefinitionStore
    from src.nl2sql.artifacts.definition_store import InMemoryDefinitionStore
    from src.nl2sql.artifacts.exploration_confirmation import (
        InMemoryExplorationConfirmationStore,
    )
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.library_control_store import ControlLibraryRepository
    from src.nl2sql.artifacts.publication import PublicationCatalogue
    from src.nl2sql.artifacts.publication_control_store import (
        ControlPublicationCatalogue,
    )

    CataloguePort = PublicationCatalogue | ControlPublicationCatalogue
    LibraryPort = InMemoryLibraryRepository | ControlLibraryRepository
    DefinitionStorePort = InMemoryDefinitionStore | ControlDefinitionStore
    ConfirmationAuditPort = (
        InMemoryConfirmationAuditStore | ControlConfirmationAuditStore
    )
    ExplorationConfirmationPort = (
        InMemoryExplorationConfirmationStore | ControlExplorationConfirmationStore
    )
