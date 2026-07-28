#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy_common.sh
source "$SCRIPT_DIR/lib/deploy_common.sh"

require_tool docker
require_tool "$PYTHON_BIN"
require_tool curl
require_deploy_environment

MANIFEST="${1:-${RELEASE_MANIFEST:-}}"
[[ -n "$MANIFEST" ]] || die "usage: rollback.sh <previous-release-manifest.yaml> --confirm-rollback"
[[ "${2:-}" == "--confirm-rollback" ]] || die "rollback requires explicit --confirm-rollback"
require_manifest "$MANIFEST"
CURRENT_MANIFEST="${CURRENT_RELEASE_MANIFEST:-}"
[[ -n "$CURRENT_MANIFEST" && -f "$CURRENT_MANIFEST" ]] || \
  die "CURRENT_RELEASE_MANIFEST must identify the release being rolled back"
current_previous="$(manifest_value "$CURRENT_MANIFEST" previous_release_id)"
target_release="$(manifest_value "$MANIFEST" release_id)"
[[ "$current_previous" == "$target_release" ]] || \
  die "rollback target is not the current release's recorded previous release"

export TTAI_IMAGE_REF="$(image_ref_from_manifest "$MANIFEST")"
compose config --quiet
compose pull api-a api-b
if [[ "${RUN_SEMANTIC_ROLLBACK:-1}" == "1" ]]; then
  semantic_release_id="$(manifest_value "$MANIFEST" semantic_release_id)"
  compose --profile ops run --rm indexer \
    python -m src.nl2sql.semantic.indexer --rollback-release "$semantic_release_id"
fi
compose up -d --no-build --wait api-a api-b
compose up -d --no-build --wait nginx
"$SCRIPT_DIR/smoke.sh"
printf 'rolled back to release %s\n' "$(manifest_value "$MANIFEST" release_id)"
