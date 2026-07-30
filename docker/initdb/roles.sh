#!/bin/sh
set -eu

: "${POSTGRES_USER:?required}"
: "${POSTGRES_DB:?required}"
: "${DB_APP_ROLE:?required}"
: "${DB_APP_PASSWORD_FILE:?required}"

read_secret() {
  secret_file="$1"
  test -f "$secret_file"
  secret_value="$(tr -d '\r\n' < "$secret_file")"
  test -n "$secret_value"
  printf '%s' "$secret_value"
}

create_login_role() {
  role_name="$1"
  password_file="$2"
  role_password="$(read_secret "$password_file")"
  psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set role_name="$role_name" --set role_password="$role_password" <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION',
  :'role_name',
  :'role_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role_name')
\gexec
SELECT format(
  'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION',
  :'role_name',
  :'role_password'
)
\gexec
SQL
}

create_extension() {
  database_name="$1"
  extension_name="$2"
  psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database_name" \
    --set extension_name="$extension_name" <<'SQL'
SELECT format('CREATE EXTENSION IF NOT EXISTS %I', :'extension_name')
\gexec
SQL
}

configure_database_privileges() {
  database_name="$1"
  psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database_name" \
    --set app_role="$DB_APP_ROLE" --set object_owner_role="$object_owner_role" <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database())
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_role')
\gexec
SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', :'app_role')
\gexec
SQL

  case "${DB_APP_PRIVILEGES:-readwrite}" in
    readonly)
      psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database_name" \
        --set app_role="$DB_APP_ROLE" --set object_owner_role="$object_owner_role" <<'SQL'
SELECT format('ALTER ROLE %I SET default_transaction_read_only = on', :'app_role')
\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', :'app_role')
\gexec
SELECT format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', :'app_role')
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO %I',
  :'object_owner_role',
  :'app_role'
)
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON SEQUENCES TO %I',
  :'object_owner_role',
  :'app_role'
)
\gexec
SQL
      ;;
    readwrite)
      psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database_name" \
        --set app_role="$DB_APP_ROLE" --set object_owner_role="$object_owner_role" <<'SQL'
SELECT format('ALTER ROLE %I RESET default_transaction_read_only', :'app_role')
\gexec
SELECT format(
  'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
  :'app_role'
)
\gexec
SELECT format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO %I', :'app_role')
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
  :'object_owner_role',
  :'app_role'
)
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I',
  :'object_owner_role',
  :'app_role'
)
\gexec
SQL
      ;;
    *)
      printf '%s\n' 'unsupported DB_APP_PRIVILEGES value' >&2
      exit 2
      ;;
  esac

  if test -n "${DB_MIGRATOR_ROLE:-}"; then
    psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database_name" \
      --set migrator_role="$DB_MIGRATOR_ROLE" <<'SQL'
SELECT format('ALTER TABLE %I.%I OWNER TO %I', n.nspname, c.relname, :'migrator_role')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
\gexec
SELECT format('ALTER SEQUENCE %I.%I OWNER TO %I', n.nspname, c.relname, :'migrator_role')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'S'
\gexec
SELECT format('ALTER VIEW %I.%I OWNER TO %I', n.nspname, c.relname, :'migrator_role')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'v'
\gexec
SELECT format('ALTER MATERIALIZED VIEW %I.%I OWNER TO %I', n.nspname, c.relname, :'migrator_role')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'm'
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'migrator_role')
\gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'migrator_role')
\gexec
SQL
  fi

  if test -n "${DB_BACKUP_ROLE:-}"; then
    psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database_name" \
      --set backup_role="$DB_BACKUP_ROLE" --set object_owner_role="$object_owner_role" <<'SQL'
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'backup_role')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'backup_role')
\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', :'backup_role')
\gexec
SELECT format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', :'backup_role')
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO %I',
  :'object_owner_role',
  :'backup_role'
)
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON SEQUENCES TO %I',
  :'object_owner_role',
  :'backup_role'
)
\gexec
SQL
  fi
}

create_login_role "$DB_APP_ROLE" "$DB_APP_PASSWORD_FILE"
object_owner_role="${DB_OBJECT_OWNER_ROLE:-$POSTGRES_USER}"

if test -n "${DB_MIGRATOR_ROLE:-}"; then
  : "${DB_MIGRATOR_PASSWORD_FILE:?required}"
  create_login_role "$DB_MIGRATOR_ROLE" "$DB_MIGRATOR_PASSWORD_FILE"
  object_owner_role="$DB_MIGRATOR_ROLE"
fi

if test -n "${DB_BACKUP_ROLE:-}"; then
  : "${DB_BACKUP_PASSWORD_FILE:?required}"
  create_login_role "$DB_BACKUP_ROLE" "$DB_BACKUP_PASSWORD_FILE"
fi

if test -n "${DB_RESTORE_TEST_DATABASE:-}"; then
  restore_role="${DB_RESTORE_ROLE:-${DB_MIGRATOR_ROLE:-}}"
  test -n "$restore_role" || {
    printf '%s\n' 'DB_RESTORE_ROLE or DB_MIGRATOR_ROLE is required for restore tests' >&2
    exit 2
  }
  case "$DB_RESTORE_TEST_DATABASE" in
    *_restore_test) ;;
    *)
      printf '%s\n' 'DB_RESTORE_TEST_DATABASE must end in _restore_test' >&2
      exit 2
      ;;
  esac
  psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set database_name="$DB_RESTORE_TEST_DATABASE" --set restore_role="$restore_role" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'database_name', :'restore_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database_name')
\gexec
SQL
fi

if test -n "${DB_REQUIRED_EXTENSION:-}"; then
  create_extension "$POSTGRES_DB" "$DB_REQUIRED_EXTENSION"
  if test -n "${DB_RESTORE_TEST_DATABASE:-}"; then
    create_extension "$DB_RESTORE_TEST_DATABASE" "$DB_REQUIRED_EXTENSION"
  fi
fi

configure_database_privileges "$POSTGRES_DB"
if test -n "${DB_RESTORE_TEST_DATABASE:-}"; then
  configure_database_privileges "$DB_RESTORE_TEST_DATABASE"
fi
