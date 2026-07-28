#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
COMPOSE_PROJECT="${TTAI_COMPOSE_PROJECT:-ttai}"
RELEASE_COMPOSE_FILE="${RELEASE_COMPOSE_FILE:-$ROOT_DIR/docker/compose.release.yml}"

die() {
  printf 'deploy error: %s\n' "$*" >&2
  exit 2
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_deploy_environment() {
  local environment="${DEPLOY_ENVIRONMENT:-}"
  [[ "$environment" == "staging" || "$environment" == "production" ]] || \
    die "DEPLOY_ENVIRONMENT must be staging or production"
  [[ "${COMPOSE_PROJECT_NAME:-$COMPOSE_PROJECT}" == "$COMPOSE_PROJECT" ]] || \
    die "COMPOSE_PROJECT_NAME must be $COMPOSE_PROJECT"
}

require_manifest() {
  local manifest="$1"
  [[ -f "$manifest" ]] || die "release manifest not found: $manifest"
  "$PYTHON_BIN" "$ROOT_DIR/scripts/release_manifest.py" verify \
    --manifest "$manifest" --compose-file "$RELEASE_COMPOSE_FILE" >/dev/null || \
    die "release manifest verification failed"
}

manifest_value() {
  "$PYTHON_BIN" "$ROOT_DIR/scripts/release_manifest.py" get --manifest "$1" --field "$2"
}

compose() {
  docker compose --project-name "$COMPOSE_PROJECT" -f "$RELEASE_COMPOSE_FILE" "$@"
}

require_absolute_backup_dir() {
  local backup_dir="$1"
  [[ "$backup_dir" = /* ]] || die "BACKUP_HOST_DIR must be an absolute path"
  [[ "$backup_dir" != "$ROOT_DIR"/*/docker/* ]] || die "backup directory may not be inside the image source tree"
  mkdir -p "$backup_dir"
}

image_ref_from_manifest() {
  local manifest="$1"
  local digest
  digest="$(manifest_value "$manifest" image_digest)"
  [[ "${TTAI_IMAGE_REPOSITORY:-}" == */* ]] || die "TTAI_IMAGE_REPOSITORY must name a registry/repository"
  printf '%s@%s\n' "$TTAI_IMAGE_REPOSITORY" "$digest"
}
