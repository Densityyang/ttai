-- Product persistence for the Definition / Artifact / Library vertical.
--
-- MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
-- metadata, the typed JSON artifacts, the hashes, the references and the
-- lifecycle.  Large CodeAct output, generated files and uploads stay out of the
-- first version ("do not persist large files by default"); this migration adds
-- no shared object storage and no new platform.
--
-- Every invariant the in-memory repositories enforce in Python is ALSO enforced
-- here, so a direct SQL writer cannot leave the product in a state the product
-- itself considers invalid.

-- ---------------------------------------------------------------------------
-- Artifacts (owner-scoped, typed JSON payload + integrity hash)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_artifacts (
  artifact_id TEXT PRIMARY KEY,
  artifact_type TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  thread_id TEXT,
  run_id TEXT,
  payload JSONB NOT NULL,
  payload_checksum TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT product_artifacts_id_shape
    CHECK (artifact_id ~ '^art_[0-9a-f]{32}$'),
  CONSTRAINT product_artifacts_type_known
    CHECK (artifact_type IN ('analysis', 'custom_definition')),
  CONSTRAINT product_artifacts_owner_bounded
    CHECK (btrim(owner_user_id) <> '' AND length(owner_user_id) <= 256),
  CONSTRAINT product_artifacts_checksum_shape
    CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
  CONSTRAINT product_artifacts_time_order
    CHECK (updated_at >= created_at)
);

-- The owner-scoped listing is the only collection read the repository exposes.
CREATE INDEX IF NOT EXISTS product_artifacts_owner_listing
  ON product_artifacts (owner_user_id, created_at DESC, artifact_id);

-- The artifact TYPE is immutable: replacing an analysis payload with a
-- custom-definition payload would leave an envelope whose declared type
-- contradicts its payload.  Identity, owner and creation time are frozen too.
CREATE OR REPLACE FUNCTION product_artifacts_guard_identity() RETURNS trigger AS $$
BEGIN
  IF NEW.artifact_id <> OLD.artifact_id
     OR NEW.artifact_type <> OLD.artifact_type
     OR NEW.owner_user_id <> OLD.owner_user_id
     OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'artifact_identity_immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.updated_at < OLD.updated_at THEN
    RAISE EXCEPTION 'artifact_updated_at_regression'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS product_artifacts_identity_immutable ON product_artifacts;
CREATE TRIGGER product_artifacts_identity_immutable
  BEFORE UPDATE ON product_artifacts
  FOR EACH ROW EXECUTE FUNCTION product_artifacts_guard_identity();

-- ---------------------------------------------------------------------------
-- Publication catalogue (immutable versions + EXPLICIT current pointer)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_publication_versions (
  identity_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  title TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  owner_label TEXT NOT NULL,
  source_label TEXT NOT NULL,
  definition_checksum TEXT NOT NULL,
  published_at TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'ratio',
  display_value TEXT,
  derived_from_identity TEXT,
  derived_from_version INTEGER,
  semantic JSONB,
  certification TEXT NOT NULL DEFAULT 'uncertified',
  certified_by TEXT,
  withdrawn BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (identity_id, version),
  CONSTRAINT product_publication_identity_not_blank
    CHECK (btrim(identity_id) <> ''),
  CONSTRAINT product_publication_version_positive CHECK (version >= 1),
  CONSTRAINT product_publication_derivation_complete CHECK (
    (derived_from_identity IS NULL AND derived_from_version IS NULL)
    OR (derived_from_identity IS NOT NULL AND derived_from_version IS NOT NULL)
  ),
  CONSTRAINT product_publication_certification_known
    CHECK (certification IN ('uncertified', 'certified')),
  -- Certification and withdrawal are SEPARATE axes; a certified row always
  -- names who certified it, so provenance cannot be dropped by a bare update.
  CONSTRAINT product_publication_certification_provenance CHECK (
    (certification = 'uncertified' AND certified_by IS NULL)
    OR (certification = 'certified' AND certified_by IS NOT NULL)
  )
);

-- The EXPLICIT current pointer.  Never max(version): the pointer is its own
-- row, and the foreign key makes "current" impossible to point at a version
-- that does not exist.
CREATE TABLE IF NOT EXISTS product_publication_identities (
  identity_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  current_version INTEGER NOT NULL,
  CONSTRAINT product_publication_current_positive CHECK (current_version >= 1),
  CONSTRAINT product_publication_current_exists
    FOREIGN KEY (identity_id, current_version)
    REFERENCES product_publication_versions (identity_id, version)
);

-- A published VERSION is IMMUTABLE.  Certification and withdrawal are the only
-- mutable axes, and the reusable semantic package can never be rewritten.
CREATE OR REPLACE FUNCTION product_publication_guard_version() RETURNS trigger AS $$
BEGIN
  IF NEW.identity_id <> OLD.identity_id
     OR NEW.version <> OLD.version
     OR NEW.title <> OLD.title
     OR NEW.owner_user_id <> OLD.owner_user_id
     OR NEW.owner_label <> OLD.owner_label
     OR NEW.source_label <> OLD.source_label
     OR NEW.definition_checksum <> OLD.definition_checksum
     OR NEW.published_at <> OLD.published_at
     OR NEW.unit <> OLD.unit
     OR NEW.display_value IS DISTINCT FROM OLD.display_value
     OR NEW.derived_from_identity IS DISTINCT FROM OLD.derived_from_identity
     OR NEW.derived_from_version IS DISTINCT FROM OLD.derived_from_version
     OR NEW.semantic IS DISTINCT FROM OLD.semantic THEN
    RAISE EXCEPTION 'published_version_is_immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS product_publication_version_immutable ON product_publication_versions;
CREATE TRIGGER product_publication_version_immutable
  BEFORE UPDATE ON product_publication_versions
  FOR EACH ROW EXECUTE FUNCTION product_publication_guard_version();

-- The current pointer is MONOTONIC: publishing a historical version preserves
-- the established pointer instead of moving it backwards.
CREATE OR REPLACE FUNCTION product_publication_guard_pointer() RETURNS trigger AS $$
BEGIN
  IF NEW.current_version < OLD.current_version THEN
    RAISE EXCEPTION 'publication_current_version_regression'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS product_publication_pointer_monotonic ON product_publication_identities;
CREATE TRIGGER product_publication_pointer_monotonic
  BEFORE UPDATE ON product_publication_identities
  FOR EACH ROW EXECUTE FUNCTION product_publication_guard_pointer();

-- ---------------------------------------------------------------------------
-- Personal library state (installs, Stars, withdrawal acknowledgement)
-- ---------------------------------------------------------------------------
-- Personal state REFERENCES the shared catalogue and never duplicates it: the
-- foreign keys below are the whole relationship, and there is no second
-- current-version map here.
CREATE TABLE IF NOT EXISTS product_library_installs (
  user_id TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  pinned BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, identity_id),
  CONSTRAINT product_library_install_user_not_blank CHECK (btrim(user_id) <> ''),
  CONSTRAINT product_library_install_version_positive CHECK (version >= 1),
  CONSTRAINT product_library_install_version_exists
    FOREIGN KEY (identity_id, version)
    REFERENCES product_publication_versions (identity_id, version)
);

-- Stars are IDENTITY scoped, so they survive an upgrade.
CREATE TABLE IF NOT EXISTS product_library_stars (
  user_id TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, identity_id),
  CONSTRAINT product_library_star_user_not_blank CHECK (btrim(user_id) <> '')
);

CREATE INDEX IF NOT EXISTS product_library_stars_identity_idx
  ON product_library_stars (identity_id);

CREATE TABLE IF NOT EXISTS product_library_withdrawal_acks (
  user_id TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, identity_id, version),
  CONSTRAINT product_library_ack_user_not_blank CHECK (btrim(user_id) <> ''),
  CONSTRAINT product_library_ack_version_positive CHECK (version >= 1),
  CONSTRAINT product_library_ack_version_exists
    FOREIGN KEY (identity_id, version)
    REFERENCES product_publication_versions (identity_id, version)
);

INSERT INTO schema_migrations (version) VALUES ('005_product_artifacts')
ON CONFLICT (version) DO NOTHING;
