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
[[ -n "$MANIFEST" ]] || die "usage: deploy.sh <release-manifest.yaml>"
require_manifest "$MANIFEST"

export TTAI_IMAGE_REF="$(image_ref_from_manifest "$MANIFEST")"
compose config --quiet
compose pull api-a api-b

# Upgrade order is deliberate: schema, semantic candidate, API instances, then Nginx.
compose --profile ops run --rm migrate
if [[ "${RUN_SEMANTIC_INDEXER:-0}" == "1" ]]; then
  compose --profile ops run --rm indexer
fi
compose up -d --no-build --wait api-a api-b
compose up -d --no-build --wait nginx
"$SCRIPT_DIR/smoke.sh"
printf 'deployed release %s\n' "$(manifest_value "$MANIFEST" release_id)"
