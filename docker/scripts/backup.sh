#!/bin/sh
set -eu

: "${DATABASE_URL_FILE:?required}"
: "${BACKUP_DIR:?required}"
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
temporary_file="$BACKUP_DIR/.${stamp}.dump.tmp"
backup_file="$BACKUP_DIR/${stamp}.dump"
pg_dump --format=custom --file "$temporary_file" "$(cat "$DATABASE_URL_FILE")"
mv "$temporary_file" "$backup_file"
cp "$backup_file" "$BACKUP_DIR/latest.dump"
find "$BACKUP_DIR" -type f -name '*.dump' ! -name 'latest.dump' -mtime +"${BACKUP_RETENTION_DAYS:-30}" -delete
