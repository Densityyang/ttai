-- Durable, owner-scoped, immutable evidence for each saved-definition rerun.
-- This is an expand-only addition.  Existing in-memory run records are
-- process-local and are intentionally not backfilled with fabricated history.

-- PostgreSQL needs an exact unique key for the owner-bound foreign key below.
CREATE UNIQUE INDEX IF NOT EXISTS custom_definitions_identity_owner_uidx
  ON custom_definitions (definition_id, owner_user_id);

CREATE TABLE IF NOT EXISTS definition_run_records (
  schema_version TEXT NOT NULL DEFAULT '1.0',
  owner_user_id TEXT NOT NULL,
  definition_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  definition_checksum TEXT NOT NULL,
  payload JSONB NOT NULL,
  payload_checksum TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT definition_run_records_identity
    PRIMARY KEY (owner_user_id, definition_id, version, run_id),
  CONSTRAINT definition_run_records_definition_owner_exists
    FOREIGN KEY (definition_id, owner_user_id)
    REFERENCES custom_definitions (definition_id, owner_user_id),
  CONSTRAINT definition_run_records_exact_version_checksum_exists
    FOREIGN KEY (definition_id, version, definition_checksum)
    REFERENCES custom_definition_versions (definition_id, version, checksum),
  CONSTRAINT definition_run_records_schema_known
    CHECK (schema_version = '1.0'),
  CONSTRAINT definition_run_records_owner_bounded
    CHECK (btrim(owner_user_id) <> '' AND length(owner_user_id) <= 256),
  CONSTRAINT definition_run_records_id_shape
    CHECK (definition_id ~ '^def_[0-9a-f]{32}$'),
  CONSTRAINT definition_run_records_version_positive
    CHECK (version >= 1),
  CONSTRAINT definition_run_records_run_id_shape
    CHECK (run_id ~ '^run_[0-9a-f]{32}$'),
  CONSTRAINT definition_run_records_definition_checksum_shape
    CHECK (definition_checksum ~ '^[0-9a-f]{64}$'),
  CONSTRAINT definition_run_records_payload_checksum_shape
    CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
  CONSTRAINT definition_run_records_payload_identity CHECK (
    jsonb_typeof(payload) = 'object'
    AND payload ?& ARRAY[
      'schema_version', 'owner_user_id', 'definition_id', 'version', 'run_id',
      'definition_checksum'
    ]
    AND payload->>'schema_version' = schema_version
    AND payload->>'owner_user_id' = owner_user_id
    AND payload->>'definition_id' = definition_id
    AND payload->>'version' ~ '^[1-9][0-9]*$'
    AND (payload->>'version')::integer = version
    AND payload->>'run_id' = run_id
    AND payload->>'definition_checksum' = definition_checksum
  )
);

CREATE INDEX IF NOT EXISTS definition_run_records_version_created_idx
  ON definition_run_records (owner_user_id, definition_id, version, created_at, run_id);

-- Immutable run evidence cannot be rewritten or deleted through the normal
-- Control application role. Exact retries are handled as INSERT/compare by the
-- store and never UPDATE the stored row.
CREATE OR REPLACE FUNCTION definition_run_records_guard_append_only()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'definition_run_record_append_only'
    USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS definition_run_records_append_only ON definition_run_records;
CREATE TRIGGER definition_run_records_append_only
  BEFORE UPDATE OR DELETE ON definition_run_records
  FOR EACH ROW EXECUTE FUNCTION definition_run_records_guard_append_only();

INSERT INTO schema_migrations (version) VALUES ('008_definition_run_records')
ON CONFLICT (version) DO NOTHING;
