#!/bin/sh
set -eu

phase="expand"
while test "$#" -gt 0; do
  case "$1" in
    --phase)
      test "$#" -ge 2 || {
        printf '%s\n' 'missing value for --phase' >&2
        exit 2
      }
      phase="$2"
      shift 2
      ;;
    *)
      printf 'unsupported migration argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if test "$phase" != "expand"; then
  printf '%s\n' 'only the backward-compatible expand phase is automated' >&2
  exit 2
fi

: "${CONTROL_MIGRATOR_DATABASE_URL_FILE:?required}"
: "${CHECKPOINT_MIGRATOR_DATABASE_URL_FILE:?required}"

require_nonempty_file() {
  required_file="$1"
  label="$2"
  if ! test -s "$required_file"; then
    printf '%s is missing or empty: %s\n' "$label" "$required_file" >&2
    exit 1
  fi
}

require_nonempty_file "$CONTROL_MIGRATOR_DATABASE_URL_FILE" "control migrator secret"
require_nonempty_file "$CHECKPOINT_MIGRATOR_DATABASE_URL_FILE" "checkpoint migrator secret"

if test "${CHECKPOINT_SNAPSHOT_REQUIRED:-false}" = "true"; then
  : "${CHECKPOINT_SNAPSHOT_FILE:?required when CHECKPOINT_SNAPSHOT_REQUIRED=true}"
  snapshot_checksum_file="${CHECKPOINT_SNAPSHOT_FILE}.sha256"
  require_nonempty_file "$CHECKPOINT_SNAPSHOT_FILE" "checkpoint snapshot"
  require_nonempty_file "$snapshot_checksum_file" "checkpoint snapshot checksum"
  expected_checksum="$(awk 'NR == 1 {print $1}' "$snapshot_checksum_file")"
  actual_checksum="$(sha256sum "$CHECKPOINT_SNAPSHOT_FILE" | awk '{print $1}')"
  if test -z "$expected_checksum"; then
    printf '%s\n' 'checkpoint snapshot checksum file is malformed' >&2
    exit 1
  fi
  if test "$expected_checksum" != "$actual_checksum"; then
    printf '%s\n' 'checkpoint snapshot checksum mismatch' >&2
    exit 1
  fi
  if ! pg_restore --list "$CHECKPOINT_SNAPSHOT_FILE" >/dev/null; then
    printf '%s\n' 'checkpoint snapshot is not a valid PostgreSQL custom archive' >&2
    exit 1
  fi
fi

printf '%s\n' 'applying control expand migrations'
alembic -c /app/docker/alembic/control.ini upgrade head
printf '%s\n' 'applying checkpoint expand migrations'
alembic -c /app/docker/alembic/checkpoint.ini upgrade head
printf '%s\n' 'applying LangGraph checkpoint migrations'
python -m src.nl2sql.infra.memory.checkpoint_migrate

alembic -c /app/docker/alembic/control.ini current --check-heads
alembic -c /app/docker/alembic/checkpoint.ini current --check-heads
