#!/bin/sh
set -eu

run_migration() {
  database_url="$1"
  migration_file="$2"
  psql "$database_url" -v ON_ERROR_STOP=1 -f "$migration_file"
}

: "${CONTROL_MIGRATOR_DATABASE_URL_FILE:?required}"
: "${CHECKPOINT_MIGRATOR_DATABASE_URL_FILE:?required}"

run_migration "$(cat "$CONTROL_MIGRATOR_DATABASE_URL_FILE")" /migrations/control/001_control_schema.sql
run_migration "$(cat "$CHECKPOINT_MIGRATOR_DATABASE_URL_FILE")" /migrations/checkpoint/001_checkpoint_schema.sql
