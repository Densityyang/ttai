from __future__ import annotations

from pathlib import Path

from scripts.release_manifest import sha256_paths, validate_manifest, verify_manifest

ROOT = Path(__file__).resolve().parents[2]


def _manifest(tmp_path: Path) -> tuple[Path, Path]:
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    manifest = tmp_path / "release.yml"
    manifest.write_text(
        """
release_id: release-1
previous_release_id: ''
git_revision: abcdef1234567
image_digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
compose_config_checksum: PLACEHOLDER
control_schema_revision: 003_audit_outbox
checkpoint_schema_revision: 001_initial
semantic_release_id: release-semantic-1
prompt_profile: prompt-v1
model_profile: model-v1
model_capability_snapshot_checksum: "0000000000000000000000000000000000000000000000000000000000000000"
policy_version: policy-v1
feature_flags: {codeact: disabled}
secret_versions: {provider_key: rotate-2026-07}
benchmark_run_ids: []
created_at: 2026-07-28T00:00:00+00:00
created_by: test
""".replace("PLACEHOLDER", sha256_paths([compose])),
        encoding="utf-8",
    )
    return manifest, compose


def test_release_manifest_verifies_config_checksum(tmp_path: Path) -> None:
    manifest, compose = _manifest(tmp_path)

    loaded = verify_manifest(manifest, [compose])

    assert loaded["release_id"] == "release-1"


def test_release_manifest_rejects_secret_values() -> None:
    errors = validate_manifest(
        {
            "release_id": "release-1",
            "previous_release_id": "",
            "git_revision": "abcdef1",
            "image_digest": "sha256:" + "0" * 64,
            "compose_config_checksum": "0" * 64,
            "control_schema_revision": "003",
            "checkpoint_schema_revision": "001",
            "semantic_release_id": "semantic-1",
            "prompt_profile": "p1",
            "model_profile": "m1",
            "model_capability_snapshot_checksum": "0" * 64,
            "policy_version": "policy-1",
            "feature_flags": {},
            "secret_versions": {"provider": "sk-live-secret"},
            "benchmark_run_ids": [],
            "created_at": "2026-07-28T00:00:00+00:00",
            "created_by": "test",
        }
    )

    assert any("secret_versions" in error for error in errors)


def test_release_scripts_have_confirmation_and_immutable_guards() -> None:
    scripts = {
        "deploy.sh": (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8"),
        "rollback.sh": (ROOT / "scripts/rollback.sh").read_text(encoding="utf-8"),
        "backup.sh": (ROOT / "scripts/backup.sh").read_text(encoding="utf-8"),
        "restore-test.sh": (ROOT / "scripts/restore-test.sh").read_text(encoding="utf-8"),
        "smoke.sh": (ROOT / "scripts/smoke.sh").read_text(encoding="utf-8"),
    }
    assert all("set -euo pipefail" in source for source in scripts.values())
    assert "--confirm-rollback" in scripts["rollback.sh"]
    assert "CURRENT_RELEASE_MANIFEST" in scripts["rollback.sh"]
    assert "--confirm-restore" in scripts["restore-test.sh"]
    common = (ROOT / "scripts/lib/deploy_common.sh").read_text(encoding="utf-8")
    assert "@" in common and "image_ref_from_manifest" in scripts["deploy.sh"]
    assert "latest" not in scripts["deploy.sh"]
    assert "legacy" not in scripts["rollback.sh"].lower()
    backup_container = (ROOT / "docker/scripts/backup.sh").read_text(encoding="utf-8")
    restore_container = (ROOT / "docker/scripts/restore-test.sh").read_text(encoding="utf-8")
    assert "latest.dump" in backup_container
    assert "--exit-on-error" in restore_container
