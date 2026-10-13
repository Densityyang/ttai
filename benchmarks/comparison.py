"""Identity-verified descriptive comparison of complete benchmark runs.

Legacy reports without replay identity remain readable, but cannot produce a
paired comparison. This module never aligns result arrays by position and does
not make significance, calibration, or release claims.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from benchmarks.metrics import BenchmarkReport, CaseResult
from benchmarks.registry import canonical_json, sha256_hex

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _PairIdentity:
    dataset_checksum: str
    case_identity_sha256: str
    mode: str
    repetition_index: int
    repetition_seed: int
    repetition_checksum: str

    def payload(self) -> dict[str, Any]:
        return {
            "dataset_checksum": self.dataset_checksum,
            "case_identity_sha256": self.case_identity_sha256,
            "mode": self.mode,
            "repetition_index": self.repetition_index,
            "repetition_seed": self.repetition_seed,
            "repetition_checksum": self.repetition_checksum,
        }

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json(self.payload()))


@dataclass(frozen=True)
class PairedComparison:
    status: Literal["PAIRED_DESCRIPTIVE", "REFUSED"]
    reason: str | None
    pair_count: int
    adjudicated_pair_count: int
    unknown_pair_count: int
    outcome_counts: dict[str, int]
    mean_paired_pass_delta: float | None
    manifest: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "pair_count": self.pair_count,
            "adjudicated_pair_count": self.adjudicated_pair_count,
            "unknown_pair_count": self.unknown_pair_count,
            "outcome_counts": dict(self.outcome_counts),
            "mean_paired_pass_delta": self.mean_paired_pass_delta,
            "manifest": dict(self.manifest),
        }


def compare_reports(
    baseline: BenchmarkReport, candidate: BenchmarkReport
) -> PairedComparison:
    """Pair exact case/revision/mode/repetition identities or refuse comparison."""

    baseline_provenance, refusal = _validated_provenance(baseline)
    if refusal:
        return _refused(refusal, baseline, candidate)
    candidate_provenance, refusal = _validated_provenance(candidate)
    if refusal:
        return _refused(refusal, baseline, candidate)
    assert baseline_provenance is not None and candidate_provenance is not None

    shared_fields = (
        "dataset_checksum",
        "case_selection_checksum",
        "selection_version",
        "seed",
        "registry_revision",
        "oracle_version",
        "assertion_version",
        "enabled_modes",
    )
    if any(
        baseline_provenance[field] != candidate_provenance[field]
        for field in shared_fields
    ):
        return _refused("shared_selection_provenance_mismatch", baseline, candidate)

    baseline_pairs, refusal = _index_results(baseline, baseline_provenance)
    if refusal:
        return _refused(refusal, baseline, candidate)
    candidate_pairs, refusal = _index_results(candidate, candidate_provenance)
    if refusal:
        return _refused(refusal, baseline, candidate)
    assert baseline_pairs is not None and candidate_pairs is not None

    if set(baseline_pairs) != set(candidate_pairs):
        missing = len(set(baseline_pairs) - set(candidate_pairs))
        extra = len(set(candidate_pairs) - set(baseline_pairs))
        return _refused(
            f"pair_set_mismatch_missing_{missing}_extra_{extra}",
            baseline,
            candidate,
        )
    if not baseline_pairs:
        return _refused("no_pairs", baseline, candidate)

    outcomes: Counter[str] = Counter()
    deltas: list[int] = []
    pair_identity_digests: list[str] = []
    for identity in sorted(baseline_pairs, key=lambda item: item.digest):
        baseline_result = baseline_pairs[identity]
        candidate_result = candidate_pairs[identity]
        baseline_pass = (
            _adjudicated_pass(baseline_result)
            if baseline.accuracy_established
            else None
        )
        candidate_pass = (
            _adjudicated_pass(candidate_result)
            if candidate.accuracy_established
            else None
        )
        if baseline_pass is None or candidate_pass is None:
            outcomes["unknown"] += 1
        elif baseline_pass and candidate_pass:
            outcomes["both_pass"] += 1
            deltas.append(0)
        elif baseline_pass:
            outcomes["baseline_only_pass"] += 1
            deltas.append(-1)
        elif candidate_pass:
            outcomes["candidate_only_pass"] += 1
            deltas.append(1)
        else:
            outcomes["both_fail"] += 1
            deltas.append(0)
        pair_identity_digests.append(identity.digest)

    shared = {field: baseline_provenance[field] for field in shared_fields}
    payload = {
        "schema_version": "1.0",
        "status": "PAIRED_DESCRIPTIVE",
        "pair_identity_policy": (
            "dataset_checksum+case_identity_sha256+mode+repetition_index+"
            "repetition_seed+repetition_checksum"
        ),
        "shared_selection": shared,
        "baseline": baseline_provenance,
        "candidate": candidate_provenance,
        "accuracy_evidence": {
            "baseline_established": baseline.accuracy_established,
            "baseline_state": baseline.accuracy_evidence_state,
            "candidate_established": candidate.accuracy_established,
            "candidate_state": candidate.accuracy_evidence_state,
        },
        "pair_count": len(pair_identity_digests),
        "pair_identity_set_sha256": sha256_hex(
            canonical_json(sorted(pair_identity_digests))
        ),
        "outcome_counts": {key: outcomes[key] for key in sorted(outcomes)},
    }
    manifest = {
        **payload,
        "manifest_sha256": sha256_hex(canonical_json(payload)),
    }
    adjudicated_count = len(deltas)
    return PairedComparison(
        status="PAIRED_DESCRIPTIVE",
        reason=None,
        pair_count=len(pair_identity_digests),
        adjudicated_pair_count=adjudicated_count,
        unknown_pair_count=outcomes["unknown"],
        outcome_counts={key: outcomes[key] for key in sorted(outcomes)},
        mean_paired_pass_delta=(
            round(sum(deltas) / adjudicated_count, 6)
            if adjudicated_count
            else None
        ),
        manifest=manifest,
    )


def _validated_provenance(
    report: BenchmarkReport,
) -> tuple[dict[str, Any] | None, str | None]:
    if report.execution_status != "COMPLETE":
        return None, "partial_run"
    evidence = report.execution_evidence
    if not isinstance(evidence, dict):
        return None, "missing_execution_evidence"
    planned = evidence.get("planned_occurrences")
    executed = evidence.get("executed_occurrences")
    unexecuted = evidence.get("unexecuted_occurrences")
    if (
        not isinstance(planned, int)
        or not isinstance(executed, int)
        or not isinstance(unexecuted, int)
        or evidence.get("status") != "COMPLETE"
        or planned != executed
        or unexecuted != 0
        or executed != len(report.results)
        or report.total_cases != executed
    ):
        return None, "incomplete_execution_coverage"
    if evidence.get("pair_identity_state") == "UNAVAILABLE":
        return None, "unavailable_pair_identity"

    manifest = report.manifest
    if not isinstance(manifest, dict):
        return None, "missing_manifest"
    required = (
        "run_id",
        "dataset_checksum",
        "git_revision",
        "case_selection_checksum",
        "selection_version",
        "seed",
        "registry_revision",
        "oracle_version",
        "assertion_version",
        "prompt_version",
        "policy_version",
        "semantic_version",
        "model_profile_version",
        "matrix",
        "enabled_modes",
    )
    if any(manifest.get(key) in (None, "") for key in required):
        return None, "incomplete_manifest_provenance"
    if not _valid_sha256(manifest.get("dataset_checksum")) or not _valid_sha256(
        manifest.get("case_selection_checksum")
    ):
        return None, "invalid_manifest_checksum"
    if (
        not isinstance(manifest.get("seed"), int)
        or manifest.get("seed", -1) < 0
        or evidence.get("selection_checksum")
        != manifest.get("case_selection_checksum")
        or evidence.get("selection_version") != manifest.get("selection_version")
    ):
        return None, "selection_provenance_mismatch"

    requested_repetitions = evidence.get("requested_repetitions")
    runs = evidence.get("runs")
    enabled_modes = manifest.get("enabled_modes")
    matrix = manifest.get("matrix")
    if (
        not isinstance(requested_repetitions, int)
        or requested_repetitions < 1
        or not isinstance(runs, list)
        or len(runs) != requested_repetitions
        or evidence.get("completed_repetitions") != requested_repetitions
        or not isinstance(enabled_modes, (list, tuple))
        or not enabled_modes
        or any(not isinstance(mode, str) or not mode for mode in enabled_modes)
        or len(set(enabled_modes)) != len(enabled_modes)
        or not isinstance(matrix, (list, tuple))
        or any(not isinstance(item, str) or not item for item in matrix)
        or len(set(matrix)) != len(matrix)
    ):
        return None, "incomplete_repetition_evidence"
    repetition_provenance: dict[int, tuple[int, str, str]] = {}
    result_counts: Counter[int] = Counter(
        result.repetition_index
        for result in report.results
        if isinstance(result.repetition_index, int)
    )
    planned_occurrences_by_repetition: Counter[int] = Counter()
    for run in runs:
        if not isinstance(run, dict):
            return None, "invalid_repetition_evidence"
        index = run.get("repetition_index")
        seed = run.get("repetition_seed")
        checksum = run.get("repetition_checksum")
        execution_run_id = run.get("execution_run_id")
        run_planned = run.get("planned_occurrences")
        run_executed = run.get("executed_occurrences")
        if (
            not isinstance(index, int)
            or index < 0
            or not isinstance(seed, int)
            or seed < 0
            or not isinstance(checksum, str)
            or not _valid_sha256(checksum)
            or not isinstance(execution_run_id, str)
            or not execution_run_id
            or run.get("status") != "COMPLETE"
            or not isinstance(run_planned, int)
            or run_planned < 1
            or not isinstance(run_executed, int)
            or run_planned != run_executed
            or run.get("unexecuted_occurrences") != 0
            or index in repetition_provenance
        ):
            return None, "invalid_repetition_evidence"
        if run_executed != result_counts[index]:
            return None, "inconsistent_repetition_result_count"
        planned_occurrences_by_repetition[index] = run_planned
        repetition_provenance[index] = (seed, checksum, execution_run_id)
    if set(repetition_provenance) != set(range(requested_repetitions)):
        return None, "incomplete_repetition_evidence"
    if (
        sum(planned_occurrences_by_repetition.values()) != planned
        or sum(result_counts.values()) != executed
    ):
        return None, "inconsistent_repetition_totals"

    provenance = {
        key: manifest[key]
        for key in required
    }
    provenance["matrix"] = list(provenance["matrix"])
    provenance["enabled_modes"] = list(provenance["enabled_modes"])
    provenance["accuracy_established"] = report.accuracy_established
    provenance["accuracy_evidence_state"] = report.accuracy_evidence_state
    legacy_extras = manifest.get("legacy_extras") or {}
    if not isinstance(legacy_extras, dict):
        return None, "invalid_manifest_provenance"
    explicit_config_checksum = legacy_extras.get("variant_config_sha256")
    if explicit_config_checksum is not None and not _valid_sha256(
        explicit_config_checksum
    ):
        return None, "invalid_runtime_config_checksum"
    provenance["runtime_config_sha256"] = (
        explicit_config_checksum
        if isinstance(explicit_config_checksum, str)
        else sha256_hex(
            canonical_json(
                {
                    key: provenance[key]
                    for key in (
                        "matrix",
                        "enabled_modes",
                        "prompt_version",
                        "policy_version",
                        "semantic_version",
                        "model_profile_version",
                    )
                }
            )
        )
    )
    provenance["requested_repetitions"] = requested_repetitions
    provenance["repetitions"] = [
        {
            "index": index,
            "seed": repetition_provenance[index][0],
            "checksum": repetition_provenance[index][1],
            "execution_run_id": repetition_provenance[index][2],
        }
        for index in range(requested_repetitions)
    ]
    return provenance, None


def _index_results(
    report: BenchmarkReport, provenance: dict[str, Any]
) -> tuple[dict[_PairIdentity, CaseResult] | None, str | None]:
    repetitions = {
        item["index"]: (item["seed"], item["checksum"], item["execution_run_id"])
        for item in provenance["repetitions"]
    }
    pairs: dict[_PairIdentity, CaseResult] = {}
    for result in report.results:
        if result.mode not in provenance["enabled_modes"]:
            return None, "result_mode_not_enabled"
        if (
            not _valid_sha256(result.case_identity_sha256)
            or result.selection_checksum != provenance["case_selection_checksum"]
            or not isinstance(result.mode, str)
            or not result.mode
            or not isinstance(result.repetition_index, int)
            or result.repetition_index not in repetitions
            or not isinstance(result.repetition_seed, int)
            or not _valid_sha256(result.repetition_checksum)
            or not isinstance(result.execution_run_id, str)
            or not result.execution_run_id
        ):
            return None, "result_missing_pair_identity"
        expected_seed, expected_checksum, expected_run_id = repetitions[
            result.repetition_index
        ]
        if (
            result.repetition_seed != expected_seed
            or result.repetition_checksum != expected_checksum
            or result.execution_run_id != expected_run_id
        ):
            return None, "result_repetition_provenance_mismatch"
        identity = _PairIdentity(
            dataset_checksum=provenance["dataset_checksum"],
            case_identity_sha256=result.case_identity_sha256,
            mode=result.mode,
            repetition_index=result.repetition_index,
            repetition_seed=result.repetition_seed,
            repetition_checksum=result.repetition_checksum,
        )
        if identity in pairs:
            return None, "duplicate_pair_identity"
        pairs[identity] = result
    return pairs, None


def _adjudicated_pass(result: CaseResult) -> bool | None:
    if result.adjudicated is True and isinstance(result.passed, bool):
        return result.passed
    return None


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _refused(
    reason: str, baseline: BenchmarkReport, candidate: BenchmarkReport
) -> PairedComparison:
    payload = {
        "schema_version": "1.0",
        "status": "REFUSED",
        "reason": reason,
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
    }
    return PairedComparison(
        status="REFUSED",
        reason=reason,
        pair_count=0,
        adjudicated_pair_count=0,
        unknown_pair_count=0,
        outcome_counts={},
        mean_paired_pass_delta=None,
        manifest={
            **payload,
            "manifest_sha256": sha256_hex(canonical_json(payload)),
        },
    )


__all__ = ["PairedComparison", "compare_reports"]
