#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy_common.sh
source "$SCRIPT_DIR/lib/deploy_common.sh"

require_tool docker
require_deploy_environment
[[ "${1:-}" == "--confirm-restore" ]] || die "restore test requires explicit --confirm-restore"
BACKUP_HOST_DIR="${BACKUP_HOST_DIR:-$ROOT_DIR/var/backups}"
require_absolute_backup_dir "$BACKUP_HOST_DIR"
test -s "$BACKUP_HOST_DIR/latest.dump" || die "latest.dump is missing"
export BACKUP_HOST_DIR
compose --profile ops run --rm restore-test
printf 'restore rehearsal completed from %s\n' "$BACKUP_HOST_DIR/latest.dump"
