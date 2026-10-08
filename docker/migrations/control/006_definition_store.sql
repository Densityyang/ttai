-- Custom Definition persistence for the Definition / Publication / Library
-- vertical.
--
-- MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
-- metadata, the typed JSON artifacts, the hashes, the references and the
-- lifecycle.  Before this migration the Definition vertical was HALF persisted:
-- artifacts, the publication catalogue and the personal library lived here,
-- while CustomDefinition / DefinitionVersion / DefinitionAxes / per-version
-- lifecycle stayed in a process-local dict.  A published version therefore
-- carried a source_definition_id that did not exist after a restart.
--
-- This migration closes that gap and, following the existing convention, turns
-- every documented invariant into a DATABASE-level guarantee, so a direct SQL
-- writer cannot leave the product in a state the product considers invalid.

-- ---------------------------------------------------------------------------
-- Definitions (stable identity + the six INDEPENDENT axes + current version)
-- ---------------------------------------------------------------------------
-- The current version payload is stored INLINE, so one row read is a complete
-- CustomDefinition and a definition is never observable without its current
-- version.  The payload duplicates the version carried by the (mutable) current
-- state on purpose: it is the projection the service reads for the axes view.
CREATE TABLE IF NOT EXISTS custom_definitions (
  definition_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL,
  confirmation TEXT NOT NULL DEFAULT 'DRAFT',
  retention TEXT NOT NULL DEFAULT 'SESSION',
  publication TEXT NOT NULL DEFAULT 'UNPUBLISHED',
  certification TEXT NOT NULL DEFAULT 'UNCERTIFIED',
  -- Governance and Authority are ORTHOGONAL to the four lifecycle axes.  A
  -- Custom Definition ORIGINAL OBJECT is noncanonical for its whole life, so the
  -- database refuses an in-place canonicalize exactly like the model does.
  governance TEXT NOT NULL DEFAULT 'NONE',
  authority TEXT NOT NULL DEFAULT 'noncanonical',
  current_version INTEGER NOT NULL,
  current_payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT custom_definitions_id_shape
    CHECK (definition_id ~ '^def_[0-9a-f]{32}$'),
  CONSTRAINT custom_definitions_owner_bounded
    CHECK (btrim(owner_user_id) <> '' AND length(owner_user_id) <= 256),
  CONSTRAINT custom_definitions_confirmation_known
    CHECK (confirmation IN ('DRAFT', 'CONFIRMED')),
  CONSTRAINT custom_definitions_retention_known
    CHECK (retention IN ('SESSION', 'SAVED')),
  CONSTRAINT custom_definitions_publication_known
    CHECK (publication IN ('UNPUBLISHED', 'PUBLISHED')),
  CONSTRAINT custom_definitions_certification_known
    CHECK (certification IN ('UNCERTIFIED', 'CERTIFIED')),
  CONSTRAINT custom_definitions_governance_known
    CHECK (governance IN ('NONE', 'GOVERNANCE_CANDIDATE', 'UNDER_REVIEW')),
  CONSTRAINT custom_definitions_authority_noncanonical
    CHECK (authority = 'noncanonical'),
  CONSTRAINT custom_definitions_current_positive CHECK (current_version >= 1),
  -- A4: the axes are INDEPENDENT fields, never a linear status, but the
  -- documented implications always hold.  Governance/Authority are ORTHOGONAL
  -- and carry no implication on the four lifecycle axes.
  CONSTRAINT custom_definitions_retention_requires_confirmation
    CHECK (retention <> 'SAVED' OR confirmation = 'CONFIRMED'),
  CONSTRAINT custom_definitions_publication_requires_saved
    CHECK (publication <> 'PUBLISHED'
           OR (retention = 'SAVED' AND confirmation = 'CONFIRMED')),
  CONSTRAINT custom_definitions_certification_requires_publication
    CHECK (certification <> 'CERTIFIED' OR publication = 'PUBLISHED'),
  -- The explicit current pointer and the inline current version must agree.
  CONSTRAINT custom_definitions_current_payload_identity CHECK (
    current_payload ? 'definition_id'
    AND current_payload ? 'version'
    AND current_payload->>'definition_id' = definition_id
    AND (current_payload->>'version')::integer = current_version
  )
);

-- The owner (and the identity and creation time) of a definition are IMMUTABLE;
-- the current-version pointer is MONOTONIC, so opening a revision never moves it
-- backwards and a definition can never be re-owned.
CREATE OR REPLACE FUNCTION custom_definitions_guard_identity() RETURNS trigger AS $$
BEGIN
  IF NEW.definition_id <> OLD.definition_id THEN
    RAISE EXCEPTION 'definition_identity_immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.owner_user_id <> OLD.owner_user_id THEN
    RAISE EXCEPTION 'definition_owner_immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'definition_created_at_immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.current_version < OLD.current_version THEN
    RAISE EXCEPTION 'definition_current_version_regression'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.updated_at < OLD.updated_at THEN
    RAISE EXCEPTION 'definition_updated_at_regression'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS custom_definitions_identity_immutable ON custom_definitions;
CREATE TRIGGER custom_definitions_identity_immutable
  BEFORE UPDATE ON custom_definitions
  FOR EACH ROW EXECUTE FUNCTION custom_definitions_guard_identity();

-- ---------------------------------------------------------------------------
-- Exact immutable versions (the semantic authority of a confirmed definition)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS custom_definition_versions (
  definition_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload JSONB NOT NULL,
  checksum TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (definition_id, version),
  CONSTRAINT custom_definition_versions_definition_exists
    FOREIGN KEY (definition_id) REFERENCES custom_definitions (definition_id),
  CONSTRAINT custom_definition_versions_positive CHECK (version >= 1),
  CONSTRAINT custom_definition_versions_checksum_shape
    CHECK (checksum ~ '^[0-9a-f]{64}$'),
  -- An exact version is only ever written after semantic closure, and its
  -- payload must describe the identity it is stored under.
  CONSTRAINT custom_definition_versions_payload_identity CHECK (
    payload ? 'definition_id'
    AND payload ? 'version'
    AND payload ? 'semantic_closed'
    AND payload->>'definition_id' = definition_id
    AND (payload->>'version')::integer = version
    AND (payload->>'semantic_closed')::boolean IS TRUE
  ),
  -- The publication reference below needs a unique (id, version, checksum), so a
  -- published version can only name a definition version whose checksum matches.
  CONSTRAINT custom_definition_versions_identity_checksum
    UNIQUE (definition_id, version, checksum)
);

-- An EXACT version is IMMUTABLE: there is no in-place rewrite, only a NEW
-- version.  This is the database-level form of the service's version boundary.
CREATE OR REPLACE FUNCTION custom_definition_versions_guard_immutable()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'exact_definition_version_immutable'
    USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS custom_definition_versions_immutable
  ON custom_definition_versions;
CREATE TRIGGER custom_definition_versions_immutable
  BEFORE UPDATE ON custom_definition_versions
  FOR EACH ROW EXECUTE FUNCTION custom_definition_versions_guard_immutable();

-- Version numbers are MONOTONIC: an exact version is exactly one past the
-- highest already confirmed, so a version can never be skipped or replayed.
CREATE OR REPLACE FUNCTION custom_definition_versions_guard_sequence()
RETURNS trigger AS $$
DECLARE
  highest INTEGER;
BEGIN
  -- A duplicate INSERT is resolved by ON CONFLICT DO NOTHING and is NOT a new
  -- version, so the sequence guard deliberately does not apply to it.  The
  -- primary key still makes a direct duplicate write fail closed.
  IF EXISTS (
    SELECT 1 FROM custom_definition_versions
     WHERE definition_id = NEW.definition_id AND version = NEW.version
  ) THEN
    RETURN NEW;
  END IF;
  SELECT COALESCE(MAX(version), 0) INTO highest
    FROM custom_definition_versions
   WHERE definition_id = NEW.definition_id;
  IF NEW.version <> highest + 1 THEN
    RAISE EXCEPTION 'definition_version_not_monotonic'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS custom_definition_versions_monotonic
  ON custom_definition_versions;
CREATE TRIGGER custom_definition_versions_monotonic
  BEFORE INSERT ON custom_definition_versions
  FOR EACH ROW EXECUTE FUNCTION custom_definition_versions_guard_sequence();

-- ---------------------------------------------------------------------------
-- Per-version PRIVATE lifecycle (survives a later revision)
-- ---------------------------------------------------------------------------
-- The custom_definitions axes are only a PROJECTION of the CURRENT version, so a
-- revision resets them.  Historical publication eligibility must nevertheless
-- stay provable for an EXACT version, which is what this table records.
CREATE TABLE IF NOT EXISTS custom_definition_lifecycles (
  definition_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  confirmation TEXT NOT NULL DEFAULT 'DRAFT',
  retention TEXT NOT NULL DEFAULT 'SESSION',
  PRIMARY KEY (definition_id, version),
  CONSTRAINT custom_definition_lifecycles_definition_exists
    FOREIGN KEY (definition_id) REFERENCES custom_definitions (definition_id),
  CONSTRAINT custom_definition_lifecycles_version_positive CHECK (version >= 1),
  CONSTRAINT custom_definition_lifecycles_confirmation_known
    CHECK (confirmation IN ('DRAFT', 'CONFIRMED')),
  CONSTRAINT custom_definition_lifecycles_retention_known
    CHECK (retention IN ('SESSION', 'SAVED')),
  CONSTRAINT custom_definition_lifecycles_retention_requires_confirmation
    CHECK (retention <> 'SAVED' OR confirmation = 'CONFIRMED')
);

-- ---------------------------------------------------------------------------
-- Publication -> Definition REFERENCE INTEGRITY (the dangling-reference fix)
-- ---------------------------------------------------------------------------
-- product_publication_versions.semantic already carries the source definition
-- id/version/checksum as JSONB VALUES, so the database could not previously
-- guarantee that the definition exists.  The three GENERATED columns below are
-- derived by the database from that JSONB, and the composite foreign key makes a
-- published version impossible to write unless the EXACT definition version
-- exists AND its recorded checksum matches.  A real foreign key also means the
-- referenced definition version cannot be deleted while a publication names it,
-- so a dangling source_definition_id is not merely rejected but unrepresentable.
--
-- Both the completeness CHECK and the foreign key are NOT VALID: rows already
-- published by 005 (legacy display/install-only fixtures, or semantic packages
-- written before definitions were persisted) are GRANDFATHERED, while every NEW
-- row is enforced.  Validating pre-existing rows is an explicit operator step,
-- not something this expand-only migration guesses at.
ALTER TABLE product_publication_versions
  ADD COLUMN IF NOT EXISTS source_definition_id TEXT
    GENERATED ALWAYS AS (semantic->>'source_definition_id') STORED;
ALTER TABLE product_publication_versions
  ADD COLUMN IF NOT EXISTS source_definition_version INTEGER
    GENERATED ALWAYS AS (
      CASE
        WHEN semantic->>'source_definition_version' ~ '^[1-9][0-9]*$'
        THEN (semantic->>'source_definition_version')::integer
        ELSE NULL
      END
    ) STORED;
ALTER TABLE product_publication_versions
  ADD COLUMN IF NOT EXISTS source_definition_checksum TEXT
    GENERATED ALWAYS AS (semantic->>'source_definition_checksum') STORED;

ALTER TABLE product_publication_versions
  DROP CONSTRAINT IF EXISTS product_publication_semantic_source_complete;
ALTER TABLE product_publication_versions
  ADD CONSTRAINT product_publication_semantic_source_complete CHECK (
    semantic IS NULL OR (
      semantic ? 'source_definition_id'
      AND COALESCE(
        semantic->>'source_definition_id' ~ '^def_[0-9a-f]{32}$', FALSE
      )
      AND semantic ? 'source_definition_version'
      AND COALESCE(
        semantic->>'source_definition_version' ~ '^[1-9][0-9]*$', FALSE
      )
      AND semantic ? 'source_definition_checksum'
      AND COALESCE(
        semantic->>'source_definition_checksum' ~ '^[0-9a-f]{64}$', FALSE
      )
    )
  ) NOT VALID;

CREATE INDEX IF NOT EXISTS product_publication_source_definition_idx
  ON product_publication_versions (
    source_definition_id, source_definition_version, source_definition_checksum
  );

ALTER TABLE product_publication_versions
  DROP CONSTRAINT IF EXISTS product_publication_source_definition_exists;
ALTER TABLE product_publication_versions
  ADD CONSTRAINT product_publication_source_definition_exists
  FOREIGN KEY (
    source_definition_id, source_definition_version, source_definition_checksum
  )
  REFERENCES custom_definition_versions (definition_id, version, checksum)
  NOT VALID;

INSERT INTO schema_migrations (version) VALUES ('006_definition_store')
ON CONFLICT (version) DO NOTHING;
