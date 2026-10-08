"""P9A selection: change -> capability -> case, reproducible and conservative.

The registry says what a case *is*; this module decides which cases a change
obliges us to run.  Three properties are non-negotiable and are each covered by
an execution-level test:

1. A change names capabilities, and only the affected cases (plus the always-on
   shared regression set) are selected.
2. Shared cases are deduplicated per (case_id, mode) -- never across modes.  The
   same case_id under QUERY and BUILD is two distinct evaluation obligations and
   dropping either would silently lose coverage.
3. When the impact scope cannot be determined, the selection EXPANDS to the full
   regression set instead of shrinking.  A selector that guesses would be worse
   than running everything.

Selection is reproducible: the same (cases, changes, seed) yields the same
checksum, and that checksum is carried into the run manifest.

Pure module: no I/O, no network, no clock.  The only randomness is a seeded
`random.Random`.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from benchmarks.registry import (
    EvalCase,
    canonical_json,
    case_set_checksum,
    dedupe_cases,
    sha256_hex,
)

SELECTION_VERSION: Final[str] = "p9a-selection-1.0"

# ── change -> capability ────────────────────────────────────────────────────
# A change id names the subsystem a PR touched.  Mapping it onto the
# capabilities whose cases could plausibly be affected is the whole point of
# selection: running everything is the fallback, not the default.
CHANGE_CAPABILITY_MAP: Final[Mapping[str, frozenset[str]]] = {
    "nl2sql.orchestration.metric_query": frozenset({"fetch", "compute"}),
    "nl2sql.orchestration.execution": frozenset({"fetch", "compute"}),
    "nl2sql.orchestration.grounding": frozenset({"fetch", "compute", "availability"}),
    "nl2sql.orchestration.analysis_evidence": frozenset({"compute"}),
    "nl2sql.orchestration.typed_runtime": frozenset({"fetch", "compute"}),
    "nl2sql.orchestration.mode_contract": frozenset(
        {"fetch", "compute", "clarification", "build"}
    ),
    "nl2sql.orchestration.decision_contract": frozenset({"clarification"}),
    "nl2sql.orchestration.planning": frozenset({"fetch", "compute"}),
    "nl2sql.orchestration.budget": frozenset({"fetch", "compute", "availability"}),
    "nl2sql.orchestration.safety": frozenset({"safety"}),
    "nl2sql.artifacts.definitions": frozenset({"build"}),
    "nl2sql.artifacts.service": frozenset({"build"}),
    "benchmarks.typed_receipts": frozenset({"fetch", "compute", "safety"}),
    "benchmarks.metrics": frozenset({"fetch", "compute", "safety", "clarification"}),
}

# Every capability a case may declare.  A change id that is already one of
# these is accepted directly, so a caller need not know the subsystem spelling.
KNOWN_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {capability for values in CHANGE_CAPABILITY_MAP.values() for capability in values}
    | {"fetch", "compute", "safety", "clarification", "availability", "build"}
)

# Cases that must ALWAYS be in a selection: a change to one capability can
# always break the shared safety / refusal / clarification contract.
SHARED_REGRESSION_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {"safety", "clarification", "availability"}
)
# NOTE: "regression" is deliberately NOT here.  In this repo that tag marks a
# statistical-regression ANALYZE capability, not a shared regression suite;
# treating it as cross-cutting would pull every statistics case into every run.
SHARED_REGRESSION_TAGS: Final[frozenset[str]] = frozenset(
    {"shared_regression", "smoke", "cross_cutting"}
)

MAPPED_REASON: Final[str] = "capability_mapped"
CONSERVATIVE_REASON: Final[str] = "conservative_full_regression"


def case_key(case: EvalCase) -> str:
    """Stable identity for a case AND its mode (never case_id alone)."""
    return f"{case.case_id}::{case.mode}"


def is_shared_regression_case(case: EvalCase) -> bool:
    """True when a case guards cross-cutting behaviour that is always re-run."""
    return (
        case.capability in SHARED_REGRESSION_CAPABILITIES
        or case.risk == "high"
        or bool(set(case.tags) & SHARED_REGRESSION_TAGS)
    )


def resolve_capabilities(
    changes: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Map change ids onto capabilities.

    Returns (capabilities, unknown_change_ids).  A change id that is neither a
    known subsystem nor a known capability is UNKNOWN: the caller must not
    guess, it must widen the selection.
    """
    capabilities: set[str] = set()
    unknown: list[str] = []
    for raw in changes:
        change = raw.strip()
        if not change:
            unknown.append(raw)
            continue
        if change in CHANGE_CAPABILITY_MAP:
            capabilities.update(CHANGE_CAPABILITY_MAP[change])
            continue
        if change in KNOWN_CAPABILITIES:
            capabilities.add(change)
            continue
        unknown.append(change)
    return tuple(sorted(capabilities)), tuple(unknown)


def _sample_sorted(
    cases: Sequence[EvalCase],
    *,
    max_cases: int | None,
    seed: int,
) -> tuple[EvalCase, ...]:
    """Deterministic, order-independent sample of a case set."""
    ordered = sorted(cases, key=lambda case: (case.case_id, case.mode))
    if max_cases is None or len(ordered) <= max_cases:
        return tuple(ordered)
    rng = random.Random(seed)
    picked = sorted(rng.sample(range(len(ordered)), max_cases))
    return tuple(ordered[index] for index in picked)


def _repetition_seed(seed: int, index: int) -> int:
    """A distinct, reproducible seed per repetition (never the clock)."""
    digest = sha256_hex(f"{SELECTION_VERSION}:{seed}:{index}")
    return int(digest[:8], 16)


@dataclass(frozen=True)
class SelectionRun:
    """One repetition of the selected case set."""

    index: int
    seed: int
    case_keys: tuple[str, ...]
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "seed": self.seed,
            "case_count": len(self.case_keys),
            "case_keys": list(self.case_keys),
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class SelectionPlan:
    """The reviewed result of one selection decision."""

    selection_version: str
    reason: str
    requested_changes: tuple[str, ...]
    unknown_changes: tuple[str, ...]
    resolved_capabilities: tuple[str, ...]
    dataset_checksum: str
    seed: int
    repetitions: int
    cases: tuple[EvalCase, ...]
    shared_regression_keys: tuple[str, ...]
    matched_keys: tuple[str, ...]
    deduplicated: int
    max_cases: int | None
    sampling_skipped: bool
    runs: tuple[SelectionRun, ...]
    checksum: str

    @property
    def conservative(self) -> bool:
        return self.reason == CONSERVATIVE_REASON

    def case_keys(self) -> tuple[str, ...]:
        return tuple(case_key(case) for case in self.cases)

    def mode_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.mode] = counts.get(case.mode, 0) + 1
        return counts

    def capability_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.capability] = counts.get(case.capability, 0) + 1
        return counts

    def case_set_checksum(self) -> str:
        return case_set_checksum(self.cases)

    def core_payload(self) -> dict[str, Any]:
        """The exact payload the selection checksum is taken over."""
        return {
            "selection_version": self.selection_version,
            "reason": self.reason,
            "requested_changes": list(self.requested_changes),
            "unknown_changes": list(self.unknown_changes),
            "resolved_capabilities": list(self.resolved_capabilities),
            "dataset_checksum": self.dataset_checksum,
            "seed": self.seed,
            "repetitions": self.repetitions,
            "case_keys": list(self.case_keys()),
            "case_set_checksum": self.case_set_checksum(),
            "runs": [run.to_dict() for run in self.runs],
        }

    def recompute_checksum(self) -> str:
        """Recompute the checksum from this plan's own published fields."""
        return sha256_hex(canonical_json(self.core_payload()))

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.core_payload(),
            "case_count": len(self.cases),
            "mode_counts": self.mode_counts(),
            "capability_counts": self.capability_counts(),
            "shared_regression_keys": list(self.shared_regression_keys),
            "matched_keys": list(self.matched_keys),
            "deduplicated": self.deduplicated,
            "max_cases": self.max_cases,
            "sampling_skipped": self.sampling_skipped,
            "checksum": self.checksum,
        }


def select_cases(
    cases: Sequence[EvalCase],
    *,
    changes: Sequence[str] = (),
    seed: int = 0,
    repetitions: int = 1,
    max_cases: int | None = None,
) -> SelectionPlan:
    """Select the cases a set of changes obliges us to run.

    Conservative by construction: an empty change list, an unknown change id or
    an unresolvable request cannot be used to shrink the run.  The full
    regression set is returned rather than a guessed subset, and `max_cases`
    is then deliberately NOT applied so the fallback cannot silently drop
    coverage.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if seed < 0:
        raise ValueError("seed must be non-negative")

    everything = dedupe_cases(cases)
    deduplicated = len(cases) - len(everything)
    dataset_checksum = case_set_checksum(everything)

    shared = tuple(case for case in everything if is_shared_regression_case(case))
    shared_keys = tuple(case_key(case) for case in shared)

    resolved, unknown = resolve_capabilities(changes)
    # An empty or unresolvable change request cannot justify narrowing: widen.
    undetermined = not changes or bool(unknown) or not resolved
    if undetermined:
        selected = everything
        reason = CONSERVATIVE_REASON
        matched: tuple[EvalCase, ...] = ()
        sampling_skipped = max_cases is not None and len(everything) > max_cases
    else:
        matched = tuple(case for case in everything if case.capability in set(resolved))
        matched = _sample_sorted(matched, max_cases=max_cases, seed=seed)
        merged = {case_key(case): case for case in (*shared, *matched)}
        selected = tuple(sorted(merged.values(), key=lambda case: (case.case_id, case.mode)))
        reason = MAPPED_REASON
        sampling_skipped = False

    runs: list[SelectionRun] = []
    ordered_selected = sorted(selected, key=lambda case: (case.case_id, case.mode))
    for index in range(repetitions):
        run_seed = _repetition_seed(seed, index)
        run_cases = _sample_sorted(ordered_selected, max_cases=max_cases, seed=run_seed)
        if undetermined:
            # The conservative branch never samples down.
            run_cases = tuple(ordered_selected)
        run_keys = tuple(case_key(case) for case in run_cases)
        runs.append(
            SelectionRun(
                index=index,
                seed=run_seed,
                case_keys=run_keys,
                checksum=sha256_hex(
                    canonical_json(
                        {
                            "selection_version": SELECTION_VERSION,
                            "index": index,
                            "seed": run_seed,
                            "case_keys": list(run_keys),
                        }
                    )
                ),
            )
        )

    plan = SelectionPlan(
        selection_version=SELECTION_VERSION,
        reason=reason,
        requested_changes=tuple(changes),
        unknown_changes=unknown,
        resolved_capabilities=resolved,
        dataset_checksum=dataset_checksum,
        seed=seed,
        repetitions=repetitions,
        cases=tuple(ordered_selected),
        shared_regression_keys=shared_keys,
        matched_keys=tuple(case_key(case) for case in matched),
        deduplicated=deduplicated,
        max_cases=max_cases,
        sampling_skipped=sampling_skipped,
        runs=tuple(runs),
        checksum="",
    )
    return replace(plan, checksum=plan.recompute_checksum())


def verify_selection_checksum(plan: SelectionPlan) -> bool:
    """True when the stored checksum still matches the plan's own content."""
    return plan.checksum == plan.recompute_checksum()


def conservative_covers_safety_subset(
    plan: SelectionPlan,
    cases: Sequence[EvalCase],
) -> bool:
    """Evidence helper: the selection never drops a shared-safety case."""
    selected = set(plan.case_keys())
    safety = {case_key(case) for case in cases if is_shared_regression_case(case)}
    return safety <= selected


def manifest_selection_fields(plan: SelectionPlan) -> dict[str, Any]:
    """The manifest projection of a selection (checksum + reproducibility)."""
    return {
        "case_selection_checksum": plan.checksum,
        "selection_version": plan.selection_version,
        "seed": plan.seed,
    }


__all__ = [
    "CHANGE_CAPABILITY_MAP",
    "CONSERVATIVE_REASON",
    "KNOWN_CAPABILITIES",
    "MAPPED_REASON",
    "SELECTION_VERSION",
    "SHARED_REGRESSION_CAPABILITIES",
    "SHARED_REGRESSION_TAGS",
    "SelectionPlan",
    "SelectionRun",
    "case_key",
    "conservative_covers_safety_subset",
    "is_shared_regression_case",
    "manifest_selection_fields",
    "resolve_capabilities",
    "select_cases",
    "verify_selection_checksum",
]
