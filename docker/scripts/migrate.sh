#!/bin/sh
set -eu

run_migration() {
  database_url="$1"
  migration_file="$2"
  psql "$database_url" -v ON_ERROR_STOP=1 -f "$migration_file"
}

: "${CONTROL_MIGRATOR_DATABASE_URL_FILE:?required}"
: "${CHECKPOINT_MIGRATOR_DATABASE_URL_FILE:?required}"

for migration_file in /migrations/control/*.sql; do
  run_migration "$(cat "$CONTROL_MIGRATOR_DATABASE_URL_FILE")" "$migration_file"
done

for migration_file in /migrations/checkpoint/*.sql; do
  run_migration "$(cat "$CHECKPOINT_MIGRATOR_DATABASE_URL_FILE")" "$migration_file"
done
