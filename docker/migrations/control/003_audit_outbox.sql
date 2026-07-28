CREATE TABLE IF NOT EXISTS audit_events (
  event_id UUID PRIMARY KEY,
  trace_id TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('query', 'retrieval', 'candidate', 'policy', 'sql', 'answer')),
  event_name TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  attributes JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS audit_events_trace_id_idx ON audit_events (trace_id, occurred_at);

CREATE TABLE IF NOT EXISTS audit_outbox (
  outbox_id UUID PRIMARY KEY,
  trace_id TEXT NOT NULL,
  event_id UUID NOT NULL REFERENCES audit_events (event_id),
  topic TEXT NOT NULL,
  payload JSONB NOT NULL,
  published_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_outbox_unpublished_idx ON audit_outbox (created_at) WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS benchmark_runs (
  run_id TEXT PRIMARY KEY,
  manifest JSONB NOT NULL,
  report JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('003_audit_outbox')
ON CONFLICT (version) DO NOTHING;
