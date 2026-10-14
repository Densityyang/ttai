-- Server-owned confirmation AUDIT persistence for Control PostgreSQL.
--
-- MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
-- metadata, the typed JSON artifacts, the hashes, the references and the
-- lifecycle.  The two records below were the LAST part of that layer still held
-- in a process-local dict: a definition confirmation recorded WHO confirmed an
-- exact version, WHEN and against WHICH server-side decision, and an exploration
-- confirmation recorded a run-scoped observation.  Both were lost on restart, so
-- "who confirmed this definition" - the exact question §8.16 P7B requires to be
-- auditable - had no durable answer.
--
-- Following the existing convention (006_definition_store.sql), every documented
-- invariant is turned into a DATABASE-level guarantee, so a direct SQL writer
-- cannot leave the product in a state the product considers invalid.
--
-- The two tables have DIFFERENT shapes on purpose:
--
--   * definition_confirmation_audit is APPEND-ONLY.  A repeated confirmation of
--     the same version appends ANOTHER record instead of erasing the previous
--     one, so the audit trail can never be rewritten.  It is bound by a
--     COMPOSITE FOREIGN KEY to the EXACT definition version it confirms, so a
--     confirmation of a version that does not exist (or whose checksum does not
--     match) is unrepresentable rather than merely rejected.
--   * exploration_confirmations is run-scoped and UNIQUE on
--     (run_id, exploration_id).  Its definition_reference is an OPTIONAL,
--     READ-ONLY provenance link to a DRAFT that was observed; a draft is NOT in
--     custom_definition_versions, so this table deliberately carries NO foreign
--     key.  Its whole meaning is the literal invariant
--     replaces_definition_confirmation = FALSE.

-- ---------------------------------------------------------------------------
-- Definition confirmation audit (append-only; bound to the exact version)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS definition_confirmation_audit (
  -- The append ORDER is the audit trail's identity: a monotonic sequence, never
  -- a rewritten key.  It is the database form of the in-memory store's list.
  audit_seq BIGSERIAL PRIMARY KEY,
  schema_version TEXT NOT NULL DEFAULT '1.0',
  definition_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  definition_checksum TEXT NOT NULL,
  -- SERVER-OWNED: the authenticated owner identity, never a client field.
  confirmed_by TEXT NOT NULL,
  -- SERVER-OWNED: the service clock.  NOT NULL, so an audit record can never be
  -- written without the actor or the time.
  confirmed_at TIMESTAMPTZ NOT NULL,
  decision_reference TEXT,
  CONSTRAINT definition_confirmation_audit_schema_version_known
    CHECK (schema_version = '1.0'),
  CONSTRAINT definition_confirmation_audit_id_shape
    CHECK (definition_id ~ '^def_[0-9a-f]{32}$'),
  CONSTRAINT definition_confirmation_audit_version_positive
    CHECK (version >= 1),
  CONSTRAINT definition_confirmation_audit_checksum_shape
    CHECK (definition_checksum ~ '^[0-9a-f]{64}$'),
  -- Server-produced audit information MUST have a value: blank is refused.
  CONSTRAINT definition_confirmation_audit_actor_present
    CHECK (btrim(confirmed_by) <> '' AND length(confirmed_by) <= 256),
  CONSTRAINT definition_confirmation_audit_reference_bounded
    CHECK (decision_reference IS NULL OR length(decision_reference) <= 256)
);

-- An audit record is APPEND-ONLY: there is no in-place rewrite and no delete.
-- This is the database-level form of "the trail cannot be rewritten by a later
-- confirmation".
CREATE OR REPLACE FUNCTION definition_confirmation_audit_guard_append_only()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'confirmation_audit_append_only'
    USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS definition_confirmation_audit_append_only
  ON definition_confirmation_audit;
CREATE TRIGGER definition_confirmation_audit_append_only
  BEFORE UPDATE OR DELETE ON definition_confirmation_audit
  FOR EACH ROW EXECUTE FUNCTION definition_confirmation_audit_guard_append_only();

CREATE INDEX IF NOT EXISTS definition_confirmation_audit_definition_idx
  ON definition_confirmation_audit (definition_id, version, audit_seq);

-- The audit record is bound to the EXACT definition version it confirms, not to
-- a mutable "current" pointer.  The composite foreign key makes a dangling or
-- checksum-mismatched confirmation impossible to write.  The constraint is
-- NOT VALID for the same reason as in 006: rows already present (there are none
-- in a fresh install, but an expand-only migration never guesses) are
-- grandfathered, while every NEW row is enforced.  Validating pre-existing rows
-- is an explicit operator step.
ALTER TABLE definition_confirmation_audit
  DROP CONSTRAINT IF EXISTS definition_confirmation_audit_version_exists;
ALTER TABLE definition_confirmation_audit
  ADD CONSTRAINT definition_confirmation_audit_version_exists
  FOREIGN KEY (definition_id, version, definition_checksum)
  REFERENCES custom_definition_versions (definition_id, version, checksum)
  NOT VALID;

-- ---------------------------------------------------------------------------
-- Run-scoped exploration confirmations (never a definition confirmation)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exploration_confirmations (
  recorded_seq BIGSERIAL PRIMARY KEY,
  schema_version TEXT NOT NULL DEFAULT '1.0',
  exploration_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  -- SERVER-OWNED: the authenticated identity, never a client field.
  confirmed_by TEXT NOT NULL,
  -- SERVER-OWNED: the service clock.  NOT NULL, exactly like the audit record.
  confirmed_at TIMESTAMPTZ NOT NULL,
  -- An OPTIONAL, READ-ONLY provenance link to the observed draft.  There is
  -- deliberately NO foreign key: a DRAFT is not an exact
  -- custom_definition_versions row, so an FK would refuse a legal record.
  definition_reference JSONB,
  -- THE invariant of this table, as a literal: an exploration confirmation never
  -- stands in for the definition's business confirmation.
  replaces_definition_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
  CONSTRAINT exploration_confirmations_run_exploration_key
    UNIQUE (run_id, exploration_id),
  CONSTRAINT exploration_confirmations_schema_version_known
    CHECK (schema_version = '1.0'),
  CONSTRAINT exploration_confirmations_id_shape
    CHECK (exploration_id ~ '^exp_[0-9a-f]{32}$'),
  CONSTRAINT exploration_confirmations_run_id_shape
    CHECK (run_id ~ '^[A-Za-z0-9_.:-]{1,128}$'),
  CONSTRAINT exploration_confirmations_subject_bounded
    CHECK (length(subject) >= 1 AND length(subject) <= 512),
  CONSTRAINT exploration_confirmations_actor_present
    CHECK (btrim(confirmed_by) <> '' AND length(confirmed_by) <= 256),
  CONSTRAINT exploration_confirmations_replaces_never
    CHECK (replaces_definition_confirmation IS FALSE),
  -- A present reference must be COMPLETE and well-shaped, exactly like the
  -- ExplorationDefinitionReference model it decodes into.
  CONSTRAINT exploration_confirmations_reference_complete CHECK (
    definition_reference IS NULL OR (
      definition_reference ? 'definition_id'
      AND definition_reference ? 'version'
      AND definition_reference ? 'definition_checksum'
      AND definition_reference ? 'semantic_closed'
      AND COALESCE(
        definition_reference->>'definition_id' ~ '^def_[0-9a-f]{32}$', FALSE
      )
      AND COALESCE(
        definition_reference->>'version' ~ '^[1-9][0-9]*$', FALSE
      )
      AND COALESCE(
        definition_reference->>'definition_checksum' ~ '^[0-9a-f]{64}$', FALSE
      )
      AND definition_reference->'semantic_closed' IN ('true'::jsonb, 'false'::jsonb)
    )
  )
);

INSERT INTO schema_migrations (version) VALUES ('007_confirmation_audit')
ON CONFLICT (version) DO NOTHING;
