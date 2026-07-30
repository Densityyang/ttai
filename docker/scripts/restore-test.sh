#!/bin/sh
set -eu

: "${CONTROL_RESTORE_DATABASE_URL_FILE:?required}"
: "${CHECKPOINT_RESTORE_DATABASE_URL_FILE:?required}"
: "${BACKUP_DIR:?required}"

read_secret() {
  secret_file="$1"
  test -f "$secret_file"
  secret_value="$(tr -d '\r\n' < "$secret_file")"
  test -n "$secret_value"
  printf '%s' "$secret_value"
}

verify_checksum() {
  backup_file="$1"
  checksum_file="${backup_file}.sha256"
  test -s "$backup_file"
  test -s "$checksum_file"
  expected_checksum="$(awk 'NR == 1 {print $1}' "$checksum_file")"
  actual_checksum="$(sha256sum "$backup_file" | awk '{print $1}')"
  test -n "$expected_checksum"
  if test "$expected_checksum" != "$actual_checksum"; then
    printf 'backup checksum mismatch: %s\n' "$backup_file" >&2
    exit 1
  fi
  pg_restore --list "$backup_file" >/dev/null
}

restore_database() {
  database_name="$1"
  database_url_file="$2"
  backup_file="$BACKUP_DIR/$database_name/latest.dump"
  database_url="$(read_secret "$database_url_file")"
  target_database="$(psql --dbname "$database_url" --no-psqlrc --tuples-only --no-align \
    --command 'SELECT current_database()')"

  case "$target_database" in
    *_restore_test) ;;
    *)
      printf 'refusing to restore into non-test database: %s\n' "$target_database" >&2
      exit 2
      ;;
  esac

  verify_checksum "$backup_file"
  pg_restore --dbname "$database_url" \
    --exit-on-error \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    --single-transaction \
    "$backup_file"

  case "$database_name" in
    control)
      psql --dbname "$database_url" --no-psqlrc --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF to_regclass('public.semantic_releases') IS NULL
     OR to_regclass('public.audit_events') IS NULL
     OR to_regclass('public.audit_outbox') IS NULL THEN
    RAISE EXCEPTION 'control restore probe failed';
  END IF;
END
$$;
SQL
      ;;
    checkpoint)
      psql --dbname "$database_url" --no-psqlrc --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF to_regclass('public.checkpoint_migrations') IS NULL
     OR to_regclass('public.checkpoints') IS NULL THEN
    RAISE EXCEPTION 'checkpoint restore probe failed';
  END IF;
END
$$;
SQL
      ;;
  esac
}

restore_database control "$CONTROL_RESTORE_DATABASE_URL_FILE"
restore_database checkpoint "$CHECKPOINT_RESTORE_DATABASE_URL_FILE"
