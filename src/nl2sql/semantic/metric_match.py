"""Pure deterministic metric identity matching over one semantic release.

The matcher is deliberately exact and closed: a question token must equal a
normalized ``metric_key``, ``display_name`` or ``asset_id`` label.  The
longest matching label wins; an equal-length tie is AMBIGUOUS.  There is no
edit distance, substring, alias-table or embedding fallback, and no I/O.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any

from src.nl2sql.semantic.metric_contract import MetricContract
from src.nl2sql.semantic.registry import SemanticDocument, SemanticRelease


def normalize_label(value: str) -> str:
    """NFKC, casefold and whitespace collapse: the one canonical label key."""

    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


@dataclass(frozen=True)
class MetricCandidate:
    """One release metric that the question selector exactly matched."""

    asset_id: str
    metric_key: str
    display_name: str
    contract: MetricContract
    document: SemanticDocument

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                normalize_label(self.asset_id),
                normalize_label(self.metric_key),
                normalize_label(self.display_name),
            )
            if value
        )


def detect_metric_candidates(
    question: str,
    release: SemanticRelease,
    permitted_asset_ids: frozenset[str] | None = None,
) -> tuple[MetricCandidate, ...]:
    """Return the exact metric candidate(s) selected by ``question``.

    Returns an empty tuple when nothing matches, one candidate for a unique
    longest match and every tied candidate when the longest match is shared.
    ``permitted_asset_ids`` is a hard policy allow-list when supplied.
    """

    tokens = [token for token in normalize_label(question).split() if token]
    if not tokens:
        return ()

    candidates = _release_candidates(release, permitted_asset_ids)
    best_length = 0
    winner_ids: set[str] = set()
    for token in tokens:
        for candidate in candidates:
            length = _match_length(token, candidate)
            if length == 0:
                continue
            if length > best_length:
                best_length = length
                winner_ids = {candidate.asset_id}
            elif length == best_length:
                winner_ids.add(candidate.asset_id)
    if best_length == 0:
        return ()
    return tuple(
        candidate for candidate in candidates if candidate.asset_id in winner_ids
    )


def _match_length(token: str, candidate: MetricCandidate) -> int:
    """Return the matched label length, or 0 when the token does not select it.

    Two selector forms are exact-matched: a bare label and the explicit
    ``metric=<asset_id|metric_key>`` prefix.  The bare-label check runs FIRST,
    so a token that is itself a label is matched as that label and the prefix
    is never stripped from it; only a token that is not a label and starts with
    ``metric=`` is compared against asset_id/metric_key after the strip.  That
    ORDERING is what keeps the two forms unambiguous for the current label data;
    it makes no claim that a free-text label could not itself look like a
    prefixed selector.
    """

    if token in candidate.labels:
        return len(token)
    prefix = "metric="
    if not token.startswith(prefix):
        return 0
    value = token[len(prefix) :]
    for label in (normalize_label(candidate.asset_id), normalize_label(candidate.metric_key)):
        if value == label:
            return len(value)
    return 0


def _release_candidates(
    release: SemanticRelease,
    permitted_asset_ids: frozenset[str] | None,
) -> tuple[MetricCandidate, ...]:
    candidates: list[MetricCandidate] = []
    seen: set[str] = set()
    for document in release.documents:
        if document.metadata.get("asset_type") != "metric":
            continue
        if document.metadata.get("status") != "active":
            continue
        if permitted_asset_ids is not None and document.document_id not in permitted_asset_ids:
            continue
        if document.document_id in seen:
            continue
        contract = _contract(document)
        if contract is None:
            continue
        seen.add(document.document_id)
        candidates.append(
            MetricCandidate(
                asset_id=document.document_id,
                metric_key=contract.metric_key,
                display_name=contract.display_name,
                contract=contract,
                document=document,
            )
        )
    candidates.sort(key=lambda candidate: candidate.asset_id)
    return tuple(candidates)


def _contract(document: SemanticDocument) -> MetricContract | None:
    raw: Any = document.metadata.get("execution_contract", "")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        contract = MetricContract.model_validate_json(raw)
    except ValueError:
        try:
            contract = MetricContract.model_validate(json.loads(raw))
        except (ValueError, TypeError):
            return None
    if contract.asset_id != document.document_id:
        return None
    return contract


__all__ = ["MetricCandidate", "detect_metric_candidates", "normalize_label"]
