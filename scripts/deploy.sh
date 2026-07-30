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
BACKUP_HOST_DIR="${BACKUP_HOST_DIR:-$ROOT_DIR/var/backups}"
require_absolute_backup_dir "$BACKUP_HOST_DIR"
export BACKUP_HOST_DIR
compose config --quiet
compose pull api-a api-b control-postgres checkpoint-postgres backup restore-test

# Upgrade order is deliberate: checkpoint snapshot, expand migrations, semantic
# candidate, API instances, then Nginx. The cloud business database stays read-only.
compose up -d --wait control-postgres checkpoint-postgres
# Re-run the idempotent role bootstrap so upgrades from pre-PR3 named volumes
# receive the separated app/migrator/backup roles as well as fresh volumes.
compose exec -T control-postgres /bin/sh /docker-entrypoint-initdb.d/010-roles.sh
compose exec -T checkpoint-postgres /bin/sh /docker-entrypoint-initdb.d/010-roles.sh
compose --profile ops run --rm backup
test -s "$BACKUP_HOST_DIR/control/latest.dump"
test -s "$BACKUP_HOST_DIR/checkpoint/latest.dump"
compose --profile ops run --rm migrate --phase expand
if [[ "${RUN_SEMANTIC_INDEXER:-0}" == "1" ]]; then
  compose --profile ops run --rm indexer
fi
compose up -d --no-build --wait api-a api-b
compose up -d --no-build --wait nginx
"$SCRIPT_DIR/smoke.sh"
printf 'deployed release %s\n' "$(manifest_value "$MANIFEST" release_id)"
