"""Authoritative in-repo binding for the V4 P1 metric inventory sources.

This module replaces the fixture-only entry point with an explicit binding to
bytes that live inside this repository:

* configs/semantic/gold/metrics holds the canonical Gold YAML copied verbatim
  from the real Gold producer
  (tt-api/src/apps/data_repository/gold/metadata/metrics);
* configs/semantic/semantic.md is the real legacy semantic reference.

Both differ from the frozen test fixtures only by location.  The module verifies
the bound bytes against a hardcoded SHA-256 table before any inventory is built,
so a tampered, truncated, renamed, or extra source fails closed with the
offending file named in the error.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from src.nl2sql.semantic.metric_inventory import (
    MetricInventorySnapshot,
    build_metric_inventory,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
AUTHORITATIVE_CANONICAL_DIR: Path = REPO_ROOT / "configs" / "semantic" / "gold" / "metrics"
AUTHORITATIVE_LEGACY_PATH: Path = REPO_ROOT / "configs" / "semantic" / "semantic.md"
GOLD_PRODUCER_RELATIVE_PATH = "tt-api/src/apps/data_repository/gold/metadata/metrics"

EXPECTED_CANONICAL_SHA256: Mapping[str, str] = {
    "complaint.yaml": "15498384e2bd404a3d0dcb379c569182dba0040ec93134b14410298890e921a0",
    "complaint_verification.yaml": (
        "a4f0e50f6b3ba37e7aca4a6fe59b205b8bd28f4237516ef312e827e28c2f3295"
    ),
    "configured_external_metrics.yaml": (
        "f70f8cff43bbf0e746c88be6f2c13e64d9f0ce26d47d49bd93c215d001814d79"
    ),
    "fault_delivery_external.yaml": (
        "416f7ea752085c74972f209bad0e29bae6228d533c715cfd6f01c2ae04ca4b49"
    ),
    "inspection.yaml": "7c95f2b4eb622a98857e67e188bab4b4beaf0b7308fa041d373f684308915e33",
    "installation.yaml": "3d228f19c6887ae44f0386680d295b4e7cc6368c90e1cb9e4cd59e7ff944d997",
    "repair_service.yaml": "f69b7479086a71e06d4f4b00823b67b64b082071994f3d8a3fa04fcad47226ad",
    "satisfaction.yaml": "452ccfd841ac5b510cf6afc4861b114fda2235bcdb7594b2c9e1de3de1b7685e",
    "single_fault.yaml": "5d4bb9d7e5b8d412eeab2711d180fddfdba61c2646994cd8539a42dba6cdb449",
    "weak_light.yaml": "7c81b4f8f69e0b60ed1d848fd7d355a4bdcefad5184b04d65358332c1871d867",
}
EXPECTED_LEGACY_SHA256 = "ba03630a273b1a907fde868b8ac55386bdd41a688aa79b6c4765338431d16f11"

_CANONICAL_GLOB = "*.yaml"
_CHUNK_SIZE = 1024 * 1024


class AuthoritativeSourceError(RuntimeError):
    """Raised when a bound authoritative source is missing, extra, or drifted."""


def verify_authoritative_sources() -> None:
    """Fail closed unless every bound source matches its locked SHA-256.

    The canonical directory must contain exactly the ten expected *.yaml files
    and the bound legacy markdown must exist.  Any missing file, extra *.yaml
    file, or digest mismatch raises and names the offending file.

    Raises:
        AuthoritativeSourceError: If a bound source is absent, unexpected, or
            does not hash to its locked value.
    """

    _verify_canonical_directory()
    _verify_file(AUTHORITATIVE_LEGACY_PATH, EXPECTED_LEGACY_SHA256)


def load_authoritative_inventory() -> MetricInventorySnapshot:
    """Verify the bound sources and build the locked metric inventory.

    Returns:
        The verified immutable inventory snapshot built from the in-repo
        authoritative canonical and legacy sources.

    Raises:
        AuthoritativeSourceError: If the bound sources fail verification.
    """

    verify_authoritative_sources()
    return build_metric_inventory(AUTHORITATIVE_CANONICAL_DIR, AUTHORITATIVE_LEGACY_PATH)


def _verify_canonical_directory() -> None:
    directory = AUTHORITATIVE_CANONICAL_DIR
    if not directory.is_dir():
        raise AuthoritativeSourceError(
            f"authoritative canonical directory is missing: {directory}"
        )
    present = sorted(item.name for item in directory.glob(_CANONICAL_GLOB))
    missing = sorted(name for name in EXPECTED_CANONICAL_SHA256 if name not in present)
    if missing:
        raise AuthoritativeSourceError(
            f"authoritative canonical file is missing: {missing[0]}"
        )
    unexpected = sorted(name for name in present if name not in EXPECTED_CANONICAL_SHA256)
    if unexpected:
        raise AuthoritativeSourceError(
            f"authoritative canonical directory has an unexpected file: {unexpected[0]}"
        )
    for name in sorted(EXPECTED_CANONICAL_SHA256):
        _verify_file(directory / name, EXPECTED_CANONICAL_SHA256[name])


def _verify_file(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise AuthoritativeSourceError(f"authoritative source file is missing: {path.name}")
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise AuthoritativeSourceError(
            "authoritative source SHA-256 mismatch for "
            f"{path.name}: expected {expected_sha256}, got {actual_sha256}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "AUTHORITATIVE_CANONICAL_DIR",
    "AUTHORITATIVE_LEGACY_PATH",
    "EXPECTED_CANONICAL_SHA256",
    "EXPECTED_LEGACY_SHA256",
    "GOLD_PRODUCER_RELATIVE_PATH",
    "REPO_ROOT",
    "AuthoritativeSourceError",
    "load_authoritative_inventory",
    "verify_authoritative_sources",
]
