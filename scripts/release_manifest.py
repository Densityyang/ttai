#!/usr/bin/env python3
"""Create and verify immutable deployment release manifests.

The manifest is deliberately data-only: credentials are represented by a
version or fingerprint, never by a secret value.  Shell deployment scripts use
this module as their single validation boundary before touching Compose.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

REQUIRED_FIELDS = {
    "release_id",
    "previous_release_id",
    "git_revision",
    "image_digest",
    "compose_config_checksum",
    "control_schema_revision",
    "checkpoint_schema_revision",
    "semantic_release_id",
    "prompt_profile",
    "model_profile",
    "model_capability_snapshot_checksum",
    "policy_version",
    "feature_flags",
    "secret_versions",
    "benchmark_run_ids",
    "created_at",
    "created_by",
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_SUSPICIOUS_SECRET = re.compile(r"(?:^|\b)(?:sk-|nvapi-|password=|secret=|bearer\s|-----begin)", re.I)


def sha256_paths(paths: list[Path]) -> str:
    """Hash an ordered set of config files with an unambiguous separator."""
    digest = hashlib.sha256()
    for index, path in enumerate(paths):
        if index:
            digest.update(b"\n")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = REQUIRED_FIELDS - manifest.keys()
    if missing:
        errors.append(f"missing fields: {', '.join(sorted(missing))}")
    unknown = set(manifest) - REQUIRED_FIELDS
    if unknown:
        errors.append(f"unknown fields: {', '.join(sorted(unknown))}")

    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        errors.append("release_id must be a lowercase immutable identifier")
    for field in ("git_revision", "control_schema_revision", "checkpoint_schema_revision", "semantic_release_id"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            errors.append(f"{field} must be non-empty")
    if not isinstance(manifest.get("image_digest"), str) or not _DIGEST.fullmatch(manifest["image_digest"]):
        errors.append("image_digest must be sha256:<64 lowercase hex chars>")
    for field in ("compose_config_checksum", "model_capability_snapshot_checksum"):
        if not isinstance(manifest.get(field), str) or not _HEX64.fullmatch(manifest[field]):
            errors.append(f"{field} must be a 64-character lowercase SHA-256")
    if not isinstance(manifest.get("feature_flags"), dict):
        errors.append("feature_flags must be a mapping")
    if not isinstance(manifest.get("secret_versions"), dict):
        errors.append("secret_versions must be a mapping of version/fingerprint values")
    else:
        for key, value in manifest["secret_versions"].items():
            if not isinstance(key, str) or not isinstance(value, (str, int)) or _SUSPICIOUS_SECRET.search(str(value)):
                errors.append(f"secret_versions.{key} must contain only a version or fingerprint")
    if not isinstance(manifest.get("benchmark_run_ids"), list) or not all(
        isinstance(item, str) and item for item in manifest.get("benchmark_run_ids", [])
    ):
        errors.append("benchmark_run_ids must be a list of non-empty IDs")
    created_at = manifest.get("created_at")
    if isinstance(created_at, datetime):
        pass
    elif not isinstance(created_at, str):
        errors.append("created_at must be an ISO-8601 timestamp")
    else:
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("created_at must be an ISO-8601 timestamp")
    if not isinstance(manifest.get("created_by"), str) or not manifest["created_by"].strip():
        errors.append("created_by must be non-empty")
    return errors


def load_manifest(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("release manifest must be a YAML mapping")
    return payload


def verify_manifest(path: Path, compose_files: list[Path] | None = None) -> dict[str, Any]:
    manifest = load_manifest(path)
    errors = validate_manifest(manifest)
    if compose_files:
        actual = sha256_paths(compose_files)
        if actual != manifest.get("compose_config_checksum"):
            errors.append("compose_config_checksum does not match the release files")
    if errors:
        raise ValueError("; ".join(errors))
    return manifest


def _parse_mapping(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"expected KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        if not key or not value:
            raise ValueError(f"expected non-empty KEY=VALUE, got {item!r}")
        result[key] = value
    return result


def create_manifest(args: argparse.Namespace) -> dict[str, Any]:
    compose_files = [Path(item) for item in args.compose_file]
    for path in compose_files:
        if not path.is_file():
            raise ValueError(f"compose file does not exist: {path}")
    manifest = {
        "release_id": args.release_id,
        "previous_release_id": args.previous_release_id,
        "git_revision": args.git_revision,
        "image_digest": args.image_digest,
        "compose_config_checksum": sha256_paths(compose_files),
        "control_schema_revision": args.control_schema_revision,
        "checkpoint_schema_revision": args.checkpoint_schema_revision,
        "semantic_release_id": args.semantic_release_id,
        "prompt_profile": args.prompt_profile,
        "model_profile": args.model_profile,
        "model_capability_snapshot_checksum": args.model_capability_snapshot_checksum,
        "policy_version": args.policy_version,
        "feature_flags": _parse_mapping(args.feature_flag),
        "secret_versions": _parse_mapping(args.secret_version),
        "benchmark_run_ids": args.benchmark_run_id,
        "created_at": args.created_at,
        "created_by": args.created_by,
    }
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("; ".join(errors))
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--compose-file", action="append", required=True)
    create.add_argument("--release-id", required=True)
    create.add_argument("--previous-release-id", default="")
    create.add_argument("--git-revision", required=True)
    create.add_argument("--image-digest", required=True)
    create.add_argument("--control-schema-revision", required=True)
    create.add_argument("--checkpoint-schema-revision", required=True)
    create.add_argument("--semantic-release-id", required=True)
    create.add_argument("--prompt-profile", required=True)
    create.add_argument("--model-profile", required=True)
    create.add_argument("--model-capability-snapshot-checksum", required=True)
    create.add_argument("--policy-version", required=True)
    create.add_argument("--feature-flag", action="append", default=[])
    create.add_argument("--secret-version", action="append", default=[])
    create.add_argument("--benchmark-run-id", action="append", default=[])
    create.add_argument("--created-at", default=datetime.now().astimezone().isoformat())
    create.add_argument("--created-by", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--compose-file", action="append", default=[])

    get = subparsers.add_parser("get")
    get.add_argument("--manifest", type=Path, required=True)
    get.add_argument("--field", required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "create":
            manifest = create_manifest(args)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
            print(args.output)
        elif args.command == "verify":
            manifest = verify_manifest(args.manifest, [Path(item) for item in args.compose_file])
            print(f"verified release {manifest['release_id']}")
        else:
            manifest = verify_manifest(args.manifest)
            value = manifest.get(args.field)
            if value is None:
                raise ValueError(f"unknown manifest field: {args.field}")
            print(value if isinstance(value, str) else yaml.safe_dump(value, sort_keys=True).strip())
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"release manifest error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
