#!/bin/sh
set -eu

: "${DATABASE_URL_FILE:?required}"
: "${BACKUP_DIR:?required}"
mkdir -p "$BACKUP_DIR"
pg_dump --format=custom --file "$BACKUP_DIR/$(date -u +%Y%m%dT%H%M%SZ).dump" "$(cat "$DATABASE_URL_FILE")"
find "$BACKUP_DIR" -type f -name '*.dump' -mtime +"${BACKUP_RETENTION_DAYS:-7}" -delete
