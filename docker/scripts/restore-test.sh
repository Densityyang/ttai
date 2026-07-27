#!/bin/sh
set -eu

: "${DATABASE_URL_FILE:?required}"
: "${BACKUP_FILE:?required}"
pg_restore --clean --if-exists --no-owner --dbname "$(cat "$DATABASE_URL_FILE")" "$BACKUP_FILE"
