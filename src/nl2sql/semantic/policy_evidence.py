"""Release-scoped, policy-scoped evidence retrieval for the ContextCompiler.

This module adapts the asynchronous production release reader
(``ControlSemanticReleasePublisher.read_active``) to the synchronous
``SemanticRegistry`` shape the in-process ``ContextCompiler`` consumes, and
supplies the one missing production collaborator of the typed QUERY path: a
concrete :class:`ReleaseScopedPolicyEvidenceProvider`.

In the current orchestration flow, ``SemanticContextResolver.resolve`` awaits
the evidence provider before it invokes the synchronous ``ContextCompiler``.
The awaited provider phase therefore reads the active release once and pins
that exact release OBJECT REFERENCE into the shared registry;
``ContextCompiler.compile`` then performs only synchronous reads of
already-bound state.  This module contains no coroutine driving.

Every returned ``Evidence.evidence_id`` is a release ``document_id``, so the
compiler's release scoping keeps it.  ``Evidence.metadata`` is built as two
distinct field sets and is not one common set:

* metric evidence carries ``asset_type="metric"``, ``domain`` and
  ``metric_key``; it has no ``relation_id``, ``relation_name`` or ``source_ref``;
* relation evidence carries ``asset_type="relation"``, ``domain``,
  ``relation_id`` and ``source_ref``; its ``relation_name`` is CONDITIONAL --
  present only when the release document provides it as a ``relation_name``
  metadata value or as content of the form ``relation <name>``.

``SemanticContextResolver`` derives domains from the shared ``domain`` field
and approved relation ids from ``asset_type="relation"`` plus ``relation_id``.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from src.nl2sql.contracts import RequestIdentity
from src.nl2sql.semantic.context_compiler import Evidence, PolicyScopedEvidence
from src.nl2sql.semantic.metric_contract import MetricContract
from src.nl2sql.semantic.metric_match import detect_metric_candidates
from src.nl2sql.semantic.registry import SemanticDocument, SemanticRelease
from src.nl2sql.semantic.registry import SemanticRegistry as _RegistryShape

ReadActive = Callable[[], Coroutine[Any, Any, SemanticRelease | None]]

# Added to PolicyScopedEvidence.degradation_flags when the question named no
# visible metric and the bounded permitted breadth was returned instead.  This
# makes the unmatched case explicit WITHOUT changing resolution_status.
#
# Dependency precision: EA2/EA3 stop in plan_node, which runs BEFORE
# validate_node, so their query_plan_proposal_failed stop reason does NOT depend
# on resolution_status.  resolution_status decides whether
# SemanticContextResolver.resolve produces a resolved ContractContextBundle for
# the plan node to read, and what PlanValidator sees at validate_node.  This
# provider keeps "resolved" so that resolved-bundle path is unchanged.
QUESTION_METRIC_UNMATCHED_FLAG = "question_metric_unmatched"


class ActiveReleaseBoundError(RuntimeError):
    """The synchronous registry was read before a release was bound."""


class ActiveReleaseRegistry(_RegistryShape):
    """The bound active-release holder behind the sync registry shape.

    The asynchronous read happens in the awaited provider phase, and ``bind()``
    stores that already-resolved release object.  ``active_release()``/``get()``
    read only the bound cached state.  This class deliberately contains no
    ``coroutine.send``, no ``run_until_complete``, no ``get_event_loop`` and no
    nested event-loop attempt: it cannot and does not drive a suspending read
    from sync code.
    """

    def __init__(self) -> None:
        super().__init__()
        self._bound = False
        self._bound_identity: tuple[str, int, str] | None = None
        self._bound_release: SemanticRelease | None = None

    def bind(self, release: SemanticRelease | None) -> None:
        """Bind one already-awaited release; the registry serves it thereafter.

        Executable invariant: the FIRST bound release OBJECT REFERENCE is
        pinned.  After that first bind, a rebind raises when EITHER the triple
        ``(release_id, version, checksum)`` differs (a different id, or a
        same-id release with a different version or checksum) OR the triple
        matches but a DIFFERENT object instance was supplied.  A same-triple
        clone is therefore rejected, and the originally bound release object
        cannot be replaced by rebind.  Rebinding the SAME object is idempotent.

        This pins the object REFERENCE only.  No claim is made that the release
        or its nested structures are deeply immutable.
        """

        identity = (
            None
            if release is None
            else (release.release_id, release.version, release.checksum)
        )
        if self._bound and self._bound_identity != identity:
            raise ActiveReleaseBoundError(
                "active release is already bound to a different release",
            )
        if self._bound and self._bound_release is not release:
            raise ActiveReleaseBoundError(
                "active release is already bound to a different object instance",
            )
        self._bound_release = release
        self._bound = True
        self._bound_identity = identity
        if release is None:
            self._releases = {}
            self._active_release_id = None
            return
        self._releases = {release.release_id: release}
        self._active_release_id = release.release_id

    @property
    def has_bound_release(self) -> bool:
        return self._bound

    def active_release(self) -> SemanticRelease | None:
        """Return the bound release, or None when it was bound as absent.

        A read before any ``bind`` is a wiring error and fails closed instead
        of silently reporting an empty release.
        """

        if not self._bound:
            raise ActiveReleaseBoundError("active release was never bound")
        return super().active_release()

    def get(self, release_id: str) -> SemanticRelease:
        if not self._bound:
            raise ActiveReleaseBoundError("active release was never bound")
        return super().get(release_id)


class ReleaseScopedPolicyEvidenceProvider:
    """Return identity-scoped, policy-scoped release evidence for one question.

    Exact behaviour:

    * the active release is awaited once per retrieval; that SAME bound release
      object instance is pinned into the injected registry and the returned
      evidence is built from it, so the resolver and the compiler observe one
      release identity ``(release_id, version, checksum)``; the reference is
      pinned, not deeply immutable;
    * a metric document is VISIBLE only when ``permitted_asset_ids`` (when
      supplied) allows its id AND the request identity satisfies the metric
      contract permission requirement (``*`` or a superset); no organization
      scope, role mapping or hierarchy is derived here;
    * a question that exactly names a visible metric narrows the evidence to
      that metric plus the visible relation its ``source_ref`` maps to; when the
      question matches several visible metrics at the same longest label length
      (an equal-length tie) the evidence instead contains EACH tied metric, and
      each of their relations;
    * a question that names no visible metric yields EVERY policy-and-identity
      permitted metric document (plus their allowed relations) and reports
      ``resolution_status == "resolved"``.  This is deliberate and bounded by
      the caller allow-list and the identity prefilter, not the whole release:
      the deterministic plan provider owns unknown-selector failure.  When the
      allow-list or the identity admits no metric at all, the result is an
      explicit ``incomplete`` + ``unresolved_slots=("metric",)``;
    * because of that deliberate breadth, ``"resolved"`` here means the
      evidence set is INTERNALLY COMPLETE and bounded by the caller allow-list
      and the identity prefilter -- it does NOT mean the question was matched
      to a metric.  Question-match is surfaced separately and explicitly:
      ``QUESTION_METRIC_UNMATCHED_FLAG`` is added to ``degradation_flags``
      whenever the question named no visible metric and the permitted breadth
      was returned instead;
    * a relation is returned only for a visible selected metric and only when
      the allow-list permits the relation id itself, so no returned evidence
      id is ever outside the caller permitted set.
    """

    def __init__(
        self,
        read_active: ReadActive,
        relation_asset_ids: Mapping[str, str],
        permitted_asset_ids: frozenset[str] | None = None,
        registry: ActiveReleaseRegistry | None = None,
    ) -> None:
        self._read_active = read_active
        self._relation_asset_ids = {
            str(key): str(value) for key, value in relation_asset_ids.items()
        }
        self._permitted_asset_ids = permitted_asset_ids
        self._registry = registry

    async def retrieve_permitted(self, *, question: str, identity: RequestIdentity) -> PolicyScopedEvidence:
        if self._registry is None:
            raise ActiveReleaseBoundError(
                "evidence provider requires the shared ActiveReleaseRegistry",
            )
        release = await self._read_active()
        # Bind BEFORE returning: the resolver calls the synchronous compiler
        # only after this await completes.
        self._registry.bind(release)
        if release is None:
            return PolicyScopedEvidence(
                embedding_available=False,
                resolution_status="incomplete",
                unresolved_slots=("semantic_release",),
                degradation_flags=("no_active_semantic_release",),
            )

        candidates, question_matched = _visible_metric_documents(
            question,
            release,
            permitted=self._permitted_asset_ids,
            identity=identity,
        )
        if not candidates:
            # No policy-and-identity permitted metric exists, so no evidence
            # can be produced without inventing identity.  Fail closed with an
            # explicit clarification slot instead of injecting the catalogue.
            return PolicyScopedEvidence(
                embedding_available=False,
                resolution_status="incomplete",
                unresolved_slots=("metric",),
                degradation_flags=("metric_identity_missing",),
            )

        lexical: list[Evidence] = []
        graph: list[Evidence] = []
        seen_relations: set[str] = set()
        for document, contract in candidates:
            lexical.append(
                Evidence(
                    evidence_id=document.document_id,
                    content=document.content,
                    source="lexical",
                    metadata={
                        "asset_type": "metric",
                        "domain": document.metadata.get("domain", contract.domain),
                        "metric_key": contract.metric_key,
                    },
                )
            )
            relation_asset_id = self._relation_asset_ids.get(contract.source_ref)
            # The allow-list covers relation evidence too: a relation of a
            # selected visible metric is only returned when the caller
            # permitted that relation id.
            if (
                relation_asset_id is None
                or relation_asset_id in seen_relations
                or not _is_permitted(relation_asset_id, self._permitted_asset_ids)
            ):
                continue
            relation_document = _document(release, relation_asset_id)
            if relation_document is None:
                continue
            seen_relations.add(relation_asset_id)
            relation_meta: dict[str, str] = {
                "asset_type": "relation",
                "domain": relation_document.metadata.get("domain", contract.domain),
                "relation_id": relation_document.document_id,
                "source_ref": contract.source_ref,
            }
            relation_name = _relation_name(relation_document)
            if relation_name:
                relation_meta["relation_name"] = relation_name
            lexical.append(
                Evidence(
                    evidence_id=relation_document.document_id,
                    content=relation_document.content,
                    source="lexical",
                    metadata=relation_meta,
                )
            )
            graph.append(
                Evidence(
                    evidence_id=relation_document.document_id,
                    content=relation_document.content,
                    source="graph",
                    metadata=relation_meta,
                )
            )

        degradation_flags = () if question_matched else (QUESTION_METRIC_UNMATCHED_FLAG,)
        return PolicyScopedEvidence(
            lexical=tuple(lexical),
            graph=tuple(graph),
            embedding_available=False,
            resolution_status="resolved",
            degradation_flags=degradation_flags,
        )


def _visible_metric_documents(
    question: str,
    release: SemanticRelease,
    *,
    permitted: frozenset[str] | None,
    identity: RequestIdentity,
) -> tuple[list[tuple[SemanticDocument, MetricContract]], bool]:
    """Return the visible metric pairs for one question and whether it matched.

    Visibility is the caller allow-list plus the already-existing request
    identity permission check against the metric contract.  An exact question
    match narrows the result and reports ``True``; otherwise every visible
    metric is returned and it reports ``False`` so the deterministic planner
    (which owns identity resolution) can fail closed on the unknown selector
    and callers can see the breadth was NOT a question match.
    """

    visible = _visible_release(release, permitted=permitted, identity=identity)
    matched = detect_metric_candidates(question, visible, permitted)
    if matched:
        return ([(candidate.document, candidate.contract) for candidate in matched], True)
    return (_all_metric_pairs(visible), False)


def _visible_release(
    release: SemanticRelease,
    *,
    permitted: frozenset[str] | None,
    identity: RequestIdentity,
) -> SemanticRelease:
    return SemanticRelease(
        release_id=release.release_id,
        version=release.version,
        checksum=release.checksum,
        state=release.state,
        documents=tuple(
            document
            for document in release.documents
            if _metric_visible(document, permitted=permitted, identity=identity)
        ),
        validation_report=release.validation_report,
        change_summary=release.change_summary,
        previous_release_id=release.previous_release_id,
        created_at=release.created_at,
        schema_version=release.schema_version,
        parser_version=release.parser_version,
        schema_snapshot_id=release.schema_snapshot_id,
        schema_snapshot_checksum=release.schema_snapshot_checksum,
        embedding_profile=release.embedding_profile,
        embedding_dimension=release.embedding_dimension,
    )


def _metric_visible(
    document: SemanticDocument,
    *,
    permitted: frozenset[str] | None,
    identity: RequestIdentity,
) -> bool:
    """Identity-scoped metric visibility, using existing identity semantics.

    A metric is visible iff the allow-list admits it (when supplied) and the
    request identity holds its declared permissions (``*`` or a superset).
    No organization scope, role mapping or hierarchy is derived.
    """

    if document.metadata.get("asset_type") != "metric":
        return False
    if document.metadata.get("status") != "active":
        return False
    if not _is_permitted(document.document_id, permitted):
        return False
    contract = _metric_contract(document)
    if contract is None:
        return False
    if "*" in identity.permissions:
        return True
    return set(contract.required_permissions) <= set(identity.permissions)


def _all_metric_pairs(
    release: SemanticRelease,
) -> list[tuple[SemanticDocument, MetricContract]]:
    """Every parseable metric document in the already-visibility-scoped release."""

    pairs: list[tuple[SemanticDocument, MetricContract]] = []
    for document in release.documents:
        contract = _metric_contract(document)
        if contract is None:
            continue
        pairs.append((document, contract))
    return pairs


def _metric_contract(document: SemanticDocument) -> MetricContract | None:
    raw: Any = document.metadata.get("execution_contract", "")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        contract = MetricContract.model_validate_json(raw)
    except ValueError:
        return None
    if contract.asset_id != document.document_id:
        return None
    return contract


def _is_permitted(asset_id: str | None, permitted: frozenset[str] | None) -> bool:
    """The allow-list predicate applied to metric and relation evidence ids."""

    return asset_id is not None and (permitted is None or asset_id in permitted)


def _document(release: SemanticRelease, document_id: str | None) -> SemanticDocument | None:
    if document_id is None:
        return None
    for document in release.documents:
        if document.document_id == document_id:
            return document
    return None


def _relation_name(document: SemanticDocument) -> str:
    payload = document.metadata.get("relation_name", "").strip()
    if payload:
        return payload
    content = document.content.strip()
    if content.startswith("relation "):
        return content[len("relation ") :].strip()
    return ""


__all__ = [
    "QUESTION_METRIC_UNMATCHED_FLAG",
    "ActiveReleaseBoundError",
    "ActiveReleaseRegistry",
    "ReleaseScopedPolicyEvidenceProvider",
]
