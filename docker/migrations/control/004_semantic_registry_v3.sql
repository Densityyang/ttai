CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Version allocation is a database concern.  Lock the table while adopting the
-- sequence so an upgrade cannot seed it behind an already committed release.
LOCK TABLE semantic_releases IN SHARE ROW EXCLUSIVE MODE;

CREATE SEQUENCE IF NOT EXISTS semantic_release_version_seq AS BIGINT;

SELECT setval(
  'semantic_release_version_seq',
  GREATEST(
    COALESCE((SELECT MAX(version) + 1 FROM semantic_releases), 1),
    (
      SELECT CASE WHEN is_called THEN last_value + 1 ELSE last_value END
      FROM semantic_release_version_seq
    )
  ),
  false
);

ALTER TABLE semantic_releases
  ALTER COLUMN version SET DEFAULT nextval('semantic_release_version_seq');

ALTER SEQUENCE semantic_release_version_seq
  OWNED BY semantic_releases.version;

CREATE TABLE IF NOT EXISTS schema_snapshots (
  snapshot_id UUID PRIMARY KEY,
  checksum TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('candidate', 'validated', 'rejected', 'retired')),
  source_identifier TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  approved_schemas TEXT[] NOT NULL DEFAULT '{}',
  relation_count INTEGER NOT NULL CHECK (relation_count >= 0),
  payload JSONB NOT NULL,
  validation_report JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  validated_at TIMESTAMPTZ
);

ALTER TABLE semantic_releases
  ADD COLUMN IF NOT EXISTS schema_version INTEGER NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS parser_version TEXT NOT NULL DEFAULT 'legacy-semantic-indexer-v1',
  ADD COLUMN IF NOT EXISTS schema_snapshot_id UUID REFERENCES schema_snapshots(snapshot_id),
  ADD COLUMN IF NOT EXISTS embedding_profile TEXT,
  ADD COLUMN IF NOT EXISTS embedding_dimension INTEGER;

ALTER TABLE semantic_releases
  ADD CONSTRAINT semantic_releases_schema_version_v3
    CHECK (schema_version = 3),
  ADD CONSTRAINT semantic_releases_embedding_dimension_positive
    CHECK (embedding_dimension IS NULL OR embedding_dimension > 0),
  ADD CONSTRAINT semantic_releases_embedding_contract_complete
    CHECK (
      (embedding_profile IS NULL AND embedding_dimension IS NULL)
      OR (embedding_profile IS NOT NULL AND embedding_dimension IS NOT NULL)
    );

CREATE TABLE IF NOT EXISTS semantic_assets (
  release_id UUID NOT NULL REFERENCES semantic_releases(release_id) ON DELETE CASCADE,
  asset_id TEXT NOT NULL,
  asset_type TEXT NOT NULL CHECK (
    asset_type IN ('domain', 'metric', 'dimension', 'relation', 'example', 'policy', 'qa', 'view')
  ),
  status TEXT NOT NULL CHECK (status IN ('active', 'retired', 'error')),
  domain TEXT NOT NULL,
  owner TEXT NOT NULL,
  sensitivity TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  lexical TSVECTOR GENERATED ALWAYS AS (
    to_tsvector('simple', asset_id || ' ' || content)
  ) STORED,
  embedding VECTOR,
  PRIMARY KEY (release_id, asset_id),
  CHECK (asset_id <> ''),
  CHECK (domain <> ''),
  CHECK (owner <> ''),
  CHECK (sensitivity <> '')
);

CREATE INDEX IF NOT EXISTS semantic_assets_lexical_idx
  ON semantic_assets USING GIN (lexical);

CREATE INDEX IF NOT EXISTS semantic_assets_domain_type_idx
  ON semantic_assets (release_id, domain, asset_type, status);

CREATE TABLE IF NOT EXISTS semantic_aliases (
  release_id UUID NOT NULL,
  asset_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'und',
  PRIMARY KEY (release_id, normalized_alias),
  FOREIGN KEY (release_id, asset_id)
    REFERENCES semantic_assets(release_id, asset_id) ON DELETE CASCADE,
  CHECK (alias <> ''),
  CHECK (normalized_alias <> '')
);

CREATE INDEX IF NOT EXISTS semantic_aliases_trgm_idx
  ON semantic_aliases USING GIN (normalized_alias gin_trgm_ops);

CREATE TABLE IF NOT EXISTS semantic_edges (
  release_id UUID NOT NULL,
  edge_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL,
  target_asset_id TEXT NOT NULL,
  edge_type TEXT NOT NULL CHECK (edge_type IN ('approved_join', 'metric_dependency', 'lineage')),
  status TEXT NOT NULL CHECK (status IN ('approved', 'retired', 'rejected')),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (release_id, edge_id),
  FOREIGN KEY (release_id, source_asset_id)
    REFERENCES semantic_assets(release_id, asset_id) ON DELETE CASCADE,
  FOREIGN KEY (release_id, target_asset_id)
    REFERENCES semantic_assets(release_id, asset_id) ON DELETE CASCADE,
  CHECK (edge_id <> ''),
  CHECK (source_asset_id <> target_asset_id)
);

CREATE INDEX IF NOT EXISTS semantic_edges_approved_idx
  ON semantic_edges (release_id, source_asset_id, target_asset_id)
  WHERE status = 'approved';

CREATE TABLE IF NOT EXISTS semantic_validation_issues (
  issue_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  release_id UUID NOT NULL REFERENCES semantic_releases(release_id) ON DELETE CASCADE,
  asset_id TEXT,
  code TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('error', 'warning')),
  message TEXT NOT NULL,
  path TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT 'unassigned',
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (code <> ''),
  CHECK (message <> '')
);

CREATE INDEX IF NOT EXISTS semantic_validation_issues_release_idx
  ON semantic_validation_issues (release_id, severity, asset_id);

CREATE TABLE IF NOT EXISTS source_freshness (
  release_id UUID NOT NULL,
  source_asset_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('fresh', 'stale', 'unknown')),
  watermark_at TIMESTAMPTZ,
  checked_at TIMESTAMPTZ NOT NULL,
  freshness_sla_seconds INTEGER NOT NULL CHECK (freshness_sla_seconds > 0),
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (release_id, source_asset_id),
  FOREIGN KEY (release_id, source_asset_id)
    REFERENCES semantic_assets(release_id, asset_id) ON DELETE CASCADE
);

-- A permanent nullable row gives first publication and every later pointer
-- move the same row-level lock.  The row is never deleted by runtime code.
ALTER TABLE semantic_release_pointers
  ALTER COLUMN release_id DROP NOT NULL;

INSERT INTO semantic_release_pointers (pointer_name, release_id)
VALUES ('active', NULL)
ON CONFLICT (pointer_name) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('004_semantic_registry_v3')
ON CONFLICT (version) DO NOTHING;
