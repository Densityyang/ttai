-- One durable winner per owner/thread/pending HITL version.  Idempotency keys
-- and request content are retained only as hashes; the response projection is
-- constrained to safe status/version fields and contains no feedback/decision.
CREATE TABLE IF NOT EXISTS hitl_action_claims (
  owner_user_id TEXT NOT NULL,
  thread_id UUID NOT NULL,
  pending_version INTEGER NOT NULL,
  idempotency_key_sha256 TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  state TEXT NOT NULL,
  response_summary JSONB,
  response_sha256 TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT hitl_action_claims_identity
    PRIMARY KEY (owner_user_id, thread_id, pending_version),
  CONSTRAINT hitl_action_claims_owner_bounded CHECK (
    btrim(owner_user_id) <> '' AND length(owner_user_id) <= 256
  ),
  CONSTRAINT hitl_action_claims_version_positive CHECK (pending_version >= 1),
  CONSTRAINT hitl_action_claims_key_checksum_shape CHECK (
    idempotency_key_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT hitl_action_claims_payload_checksum_shape CHECK (
    payload_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT hitl_action_claims_state_known CHECK (
    state IN ('claimed', 'completed')
  ),
  CONSTRAINT hitl_action_claims_response_state_coherent CHECK (
    (state = 'claimed' AND response_summary IS NULL AND response_sha256 IS NULL)
    OR (
      state = 'completed'
      AND jsonb_typeof(response_summary) = 'object'
      AND response_sha256 ~ '^[0-9a-f]{64}$'
      AND response_summary ? 'status'
      AND response_summary ? 'version'
      AND response_summary - ARRAY[
        'target_type', 'status', 'version', 'decision_status',
        'continuation_status'
      ] = '{}'::jsonb
      AND response_summary->>'status' IN (
        'approved', 'modified', 'rejected', 'cancelled', 'recorded'
      )
      AND response_summary->>'version' ~ '^[1-9][0-9]*$'
      AND (
        response_summary->>'target_type' IS NULL
        OR response_summary->>'target_type' IN ('legacy_hitl', 'typed_decision')
      )
      AND (
        response_summary->>'target_type' IS DISTINCT FROM 'typed_decision'
        OR (
          response_summary->>'status' = 'recorded'
          AND response_summary->>'decision_status' IN (
            'resolved_pending_revalidation', 'choose_recorded_no_continuation',
            'rejected', 'cancelled'
          )
          AND response_summary->>'continuation_status' IN (
            'resolved_pending_revalidation',
            'resolved_pending_current_authorization',
            'resolved_continuing_current_authorization',
            'current_authorization_unavailable', 'current_authorization_denied',
            'continuation_execution_plan_denied', 'continuation_stopped',
            'revalidation_denied', 'awaiting_decision',
            'choose_recorded_no_continuation', 'rejected', 'cancelled', 'invalid'
          )
        )
        AND (
          response_summary->>'target_type' = 'typed_decision'
          OR (
            response_summary->>'decision_status' IS NULL
            AND response_summary->>'continuation_status' IS NULL
          )
        )
      )
    )
  ),
  CONSTRAINT hitl_action_claims_timestamps_monotonic CHECK (
    updated_at >= created_at
  )
);

-- A claim may transition to a completed safe response exactly once.  Its
-- request identity and hashes remain immutable, and completed rows cannot be
-- rewritten or deleted through the normal Control application role.
CREATE OR REPLACE FUNCTION hitl_action_claims_transition_guard()
RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'hitl_action_claim_delete_forbidden'
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.state <> 'claimed'
    OR NEW.state <> 'completed'
    OR NEW.owner_user_id <> OLD.owner_user_id
    OR NEW.thread_id <> OLD.thread_id
    OR NEW.pending_version <> OLD.pending_version
    OR NEW.idempotency_key_sha256 <> OLD.idempotency_key_sha256
    OR NEW.payload_sha256 <> OLD.payload_sha256
    OR NEW.response_summary IS NULL
    OR NEW.response_sha256 IS NULL
    OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'hitl_action_claim_transition_invalid'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS hitl_action_claims_transition_guard_trigger
  ON hitl_action_claims;
CREATE TRIGGER hitl_action_claims_transition_guard_trigger
  BEFORE UPDATE OR DELETE ON hitl_action_claims
  FOR EACH ROW EXECUTE FUNCTION hitl_action_claims_transition_guard();

INSERT INTO schema_migrations (version) VALUES ('009_hitl_action_claims')
ON CONFLICT (version) DO NOTHING;
