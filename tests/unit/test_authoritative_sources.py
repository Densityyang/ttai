"""Focused P1-S0 tests for the authoritative in-repo source binding."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from src.nl2sql.semantic import authoritative_sources
from src.nl2sql.semantic.authoritative_sources import (
    AUTHORITATIVE_CANONICAL_DIR,
    AUTHORITATIVE_LEGACY_PATH,
    EXPECTED_CANONICAL_SHA256,
    EXPECTED_LEGACY_SHA256,
    GOLD_PRODUCER_RELATIVE_PATH,
    REPO_ROOT,
    AuthoritativeSourceError,
    load_authoritative_inventory,
    verify_authoritative_sources,
)
from src.nl2sql.semantic.metric_inventory import build_metric_inventory
from src.nl2sql.semantic.planner_metric_projection import project_metric_inventory

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "v4_p1" / "authoritative"
FIXTURE_CANONICAL_DIR = FIXTURE_ROOT / "canonical"
FIXTURE_LEGACY_PATH = FIXTURE_ROOT / "legacy" / "semantic.md"

EXPECTED_INVENTORY_FINGERPRINT = (
    "36d0d2d27d8a426a18f40018c163abe9525b636d3f2f6531328e38e2728c8b8a"
)
EXPECTED_PROJECTION_FINGERPRINT = (
    "3c6a43c11fe8deb1218d79b4df7196082fe49ce027b8137a19d5e5858fcecd4d"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def bound_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Redirect the binding to a disposable byte copy of the real sources."""

    canonical = tmp_path / "canonical"
    shutil.copytree(AUTHORITATIVE_CANONICAL_DIR, canonical)
    legacy = tmp_path / "semantic.md"
    shutil.copy2(AUTHORITATIVE_LEGACY_PATH, legacy)
    monkeypatch.setattr(authoritative_sources, "AUTHORITATIVE_CANONICAL_DIR", canonical)
    monkeypatch.setattr(authoritative_sources, "AUTHORITATIVE_LEGACY_PATH", legacy)
    return canonical, legacy


def test_paths_are_derived_from_the_repository_root() -> None:
    assert REPO_ROOT == Path(__file__).resolve().parents[2]
    assert AUTHORITATIVE_CANONICAL_DIR == REPO_ROOT / "configs" / "semantic" / "gold" / "metrics"
    assert AUTHORITATIVE_LEGACY_PATH == REPO_ROOT / "configs" / "semantic" / "semantic.md"
    assert GOLD_PRODUCER_RELATIVE_PATH == "tt-api/src/apps/data_repository/gold/metadata/metrics"


def test_bound_canonical_directory_has_exactly_the_ten_expected_yaml_files() -> None:
    names = sorted(item.name for item in AUTHORITATIVE_CANONICAL_DIR.glob("*.yaml"))

    assert len(EXPECTED_CANONICAL_SHA256) == 10
    assert names == sorted(EXPECTED_CANONICAL_SHA256)


def test_bound_canonical_bytes_match_locked_table_and_frozen_fixture() -> None:
    for name, expected_sha256 in sorted(EXPECTED_CANONICAL_SHA256.items()):
        assert _sha256(AUTHORITATIVE_CANONICAL_DIR / name) == expected_sha256, name
        assert _sha256(FIXTURE_CANONICAL_DIR / name) == expected_sha256, name


def test_bound_legacy_bytes_match_locked_table_and_frozen_fixture() -> None:
    assert _sha256(AUTHORITATIVE_LEGACY_PATH) == EXPECTED_LEGACY_SHA256
    assert _sha256(FIXTURE_LEGACY_PATH) == EXPECTED_LEGACY_SHA256


def test_verify_accepts_the_bound_sources() -> None:
    assert verify_authoritative_sources() is None


def test_bound_sources_reproduce_locked_inventory_fingerprint() -> None:
    snapshot = load_authoritative_inventory()
    fixture_snapshot = build_metric_inventory(FIXTURE_CANONICAL_DIR, FIXTURE_LEGACY_PATH)

    assert snapshot.fingerprint == EXPECTED_INVENTORY_FINGERPRINT
    assert fixture_snapshot.fingerprint == EXPECTED_INVENTORY_FINGERPRINT


def test_bound_sources_reproduce_locked_planner_projection_fingerprint() -> None:
    projection = project_metric_inventory(load_authoritative_inventory())

    assert projection.inventory_fingerprint == EXPECTED_INVENTORY_FINGERPRINT
    assert projection.fingerprint == EXPECTED_PROJECTION_FINGERPRINT


def test_verify_rejects_a_tampered_canonical_copy(bound_copy: tuple[Path, Path]) -> None:
    canonical, _legacy = bound_copy
    verify_authoritative_sources()

    tampered = canonical / "complaint.yaml"
    tampered.write_bytes(tampered.read_bytes() + b"\n# tampered\n")

    with pytest.raises(AuthoritativeSourceError, match="complaint.yaml"):
        verify_authoritative_sources()


def test_verify_rejects_a_missing_canonical_file(bound_copy: tuple[Path, Path]) -> None:
    canonical, _legacy = bound_copy
    (canonical / "weak_light.yaml").unlink()

    with pytest.raises(AuthoritativeSourceError, match="weak_light.yaml"):
        verify_authoritative_sources()


def test_verify_rejects_an_unexpected_canonical_yaml_file(bound_copy: tuple[Path, Path]) -> None:
    canonical, _legacy = bound_copy
    (canonical / "extra_metric.yaml").write_bytes(b"metrics: []\n")

    with pytest.raises(AuthoritativeSourceError, match="extra_metric.yaml"):
        verify_authoritative_sources()


def test_verify_rejects_a_tampered_legacy_copy(bound_copy: tuple[Path, Path]) -> None:
    _canonical, legacy = bound_copy
    legacy.write_bytes(legacy.read_bytes() + b"\n<!-- tampered -->\n")

    with pytest.raises(AuthoritativeSourceError, match="semantic.md"):
        verify_authoritative_sources()


def test_verify_rejects_a_missing_legacy_source(bound_copy: tuple[Path, Path]) -> None:
    _canonical, legacy = bound_copy
    legacy.unlink()

    with pytest.raises(AuthoritativeSourceError, match="semantic.md"):
        verify_authoritative_sources()


def test_load_fails_closed_on_a_tampered_canonical_copy(bound_copy: tuple[Path, Path]) -> None:
    canonical, _legacy = bound_copy
    tampered = canonical / "inspection.yaml"
    tampered.write_bytes(tampered.read_bytes() + b"# tampered\n")

    with pytest.raises(AuthoritativeSourceError, match="inspection.yaml"):
        load_authoritative_inventory()
