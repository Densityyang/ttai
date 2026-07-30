#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy_common.sh
source "$SCRIPT_DIR/lib/deploy_common.sh"

require_tool docker
require_deploy_environment
BACKUP_HOST_DIR="${BACKUP_HOST_DIR:-$ROOT_DIR/var/backups}"
require_absolute_backup_dir "$BACKUP_HOST_DIR"
export BACKUP_HOST_DIR
compose --profile ops run --rm backup
test -s "$BACKUP_HOST_DIR/control/latest.dump"
test -s "$BACKUP_HOST_DIR/checkpoint/latest.dump"
test -s "$BACKUP_HOST_DIR/control/latest.dump.sha256"
test -s "$BACKUP_HOST_DIR/checkpoint/latest.dump.sha256"
printf 'control and checkpoint backups written to %s\n' "$BACKUP_HOST_DIR"
