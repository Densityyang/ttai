#!/bin/sh
set -eu

: "${CONTROL_BACKUP_DATABASE_URL_FILE:?required}"
: "${CHECKPOINT_BACKUP_DATABASE_URL_FILE:?required}"
: "${BACKUP_DIR:?required}"
retention_days="${BACKUP_RETENTION_DAYS:-7}"
case "$retention_days" in
  ''|*[!0-9]*)
    printf '%s\n' 'BACKUP_RETENTION_DAYS must be a non-negative integer' >&2
    exit 2
    ;;
esac

umask 077
mkdir -p "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.backup.lock"
if ! flock --nonblock 9; then
  printf '%s\n' 'another control/checkpoint backup is already running' >&2
  exit 1
fi
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

read_secret() {
  secret_file="$1"
  test -f "$secret_file"
  secret_value="$(tr -d '\r\n' < "$secret_file")"
  test -n "$secret_value"
  printf '%s' "$secret_value"
}

backup_database() {
  database_name="$1"
  database_url_file="$2"
  target_dir="$BACKUP_DIR/$database_name"
  temporary_file="$target_dir/.${stamp}.dump.tmp"
  backup_file="$target_dir/${stamp}.dump"
  database_url="$(read_secret "$database_url_file")"

  mkdir -p "$target_dir"
  if test "$database_name" = "control"; then
    pg_dump --dbname "$database_url" \
      --format=custom \
      --no-owner \
      --no-privileges \
      --exclude-extension=vector \
      --file "$temporary_file"
  else
    pg_dump --dbname "$database_url" \
      --format=custom \
      --no-owner \
      --no-privileges \
      --file "$temporary_file"
  fi
  pg_restore --list "$temporary_file" >/dev/null
  checksum="$(sha256sum "$temporary_file" | awk '{print $1}')"
  mv "$temporary_file" "$backup_file"
  printf '%s  %s\n' "$checksum" "$(basename "$backup_file")" > "${backup_file}.sha256"
}

promote_latest() {
  database_name="$1"
  target_dir="$BACKUP_DIR/$database_name"
  backup_file="$target_dir/${stamp}.dump"
  checksum="$(awk 'NR == 1 {print $1}' "${backup_file}.sha256")"

  cp "$backup_file" "$target_dir/.latest.dump.tmp"
  mv "$target_dir/.latest.dump.tmp" "$target_dir/latest.dump"
  printf '%s  latest.dump\n' "$checksum" > "$target_dir/.latest.dump.sha256.tmp"
  mv "$target_dir/.latest.dump.sha256.tmp" "$target_dir/latest.dump.sha256"
  printf '{"database":"%s","created_at":"%s","dump":"%s","sha256":"%s"}\n' \
    "$database_name" "$stamp" "${stamp}.dump" "$checksum" > "$target_dir/.latest.manifest.tmp"
  mv "$target_dir/.latest.manifest.tmp" "$target_dir/latest.manifest"

  find "$target_dir" -type f -name '*.dump' ! -name 'latest.dump' \
    -mtime +"$retention_days" -delete
  find "$target_dir" -type f -name '*.dump.sha256' ! -name 'latest.dump.sha256' \
    -mtime +"$retention_days" -delete
}

backup_database control "$CONTROL_BACKUP_DATABASE_URL_FILE"
backup_database checkpoint "$CHECKPOINT_BACKUP_DATABASE_URL_FILE"
promote_latest control
promote_latest checkpoint
