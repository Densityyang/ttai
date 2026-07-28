CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS semantic_releases (
  release_id UUID PRIMARY KEY,
  version BIGINT NOT NULL UNIQUE,
  checksum TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('draft', 'validated', 'active', 'retired')),
  validation_report JSONB,
  change_summary TEXT NOT NULL DEFAULT '',
  previous_release_id UUID REFERENCES semantic_releases(release_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  validated_at TIMESTAMPTZ,
  activated_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS semantic_releases_one_active
  ON semantic_releases ((state)) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS semantic_documents (
  release_id UUID NOT NULL REFERENCES semantic_releases(release_id) ON DELETE CASCADE,
  document_id TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  lexical TSVECTOR NOT NULL,
  embedding VECTOR,
  PRIMARY KEY (release_id, document_id)
);

CREATE INDEX IF NOT EXISTS semantic_documents_lexical_idx
  ON semantic_documents USING GIN (lexical);

CREATE TABLE IF NOT EXISTS semantic_release_pointers (
  pointer_name TEXT PRIMARY KEY CHECK (pointer_name = 'active'),
  release_id UUID NOT NULL REFERENCES semantic_releases(release_id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('002_semantic_registry')
ON CONFLICT (version) DO NOTHING;
