#!/bin/sh
set -eu

: "${DATABASE_URL_FILE:?required}"
: "${BACKUP_FILE:?required}"
test -s "$BACKUP_FILE"
pg_restore --exit-on-error --clean --if-exists --no-owner --dbname "$(cat "$DATABASE_URL_FILE")" "$BACKUP_FILE"
