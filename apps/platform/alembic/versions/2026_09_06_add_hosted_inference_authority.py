"""Native hosted inference authority and conservative dispatch ledger.

Revision ID: e0c6f34b18d2
Revises: d9b5ea2f3c71
"""

from alembic import op

revision = "e0c6f34b18d2"
down_revision = "d9b5ea2f3c71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = """
    CREATE TABLE coding_hosted_inference_grants (
    grant_id UUID NOT NULL,
    evaluation_id UUID NOT NULL,
    attempt_id UUID NOT NULL,
    worker_id UUID NOT NULL,
    assignment_sha256 TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    execution_profile_sha256 TEXT NOT NULL,
    policy JSONB NOT NULL,
    request_limit INTEGER NOT NULL,
    prompt_limit BIGINT NOT NULL,
    completion_limit BIGINT NOT NULL,
    cost_limit BIGINT NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    shadow_only BOOLEAN NOT NULL,
    weight_eligible BOOLEAN NOT NULL,
    CONSTRAINT pk_coding_hosted_inference_grants PRIMARY KEY (grant_id),
    CONSTRAINT fk_coding_hosted_inference_grants_evaluation_id_coding__64c4 FOREIGN
      KEY(evaluation_id) REFERENCES coding_hosted_assignments (evaluation_id) ON DELETE
      RESTRICT,
    CONSTRAINT ck_coding_hosted_inference_grants_hosted_inference_grant_bounds CHECK
      (shadow_only AND NOT weight_eligible AND request_limit BETWEEN 1 AND 256 AND
      prompt_limit BETWEEN 1 AND 2250000 AND completion_limit BETWEEN 1 AND 250000 AND
      cost_limit BETWEEN 1 AND 100000000 AND expires_at > created_at AND (revoked_at IS
      NULL OR revoked_at >= created_at)),
    CONSTRAINT ck_coding_hosted_inference_grants_hosted_inference_gran_7930 CHECK
      (assignment_sha256 ~ '^[0-9a-f]{64}$' AND policy_sha256 ~ '^[0-9a-f]{64}$' AND
      execution_profile_sha256 ~ '^[0-9a-f]{64}$' AND jsonb_typeof(policy)= 'object'
      AND octet_length(policy::text)<=16384),
    CONSTRAINT uq_coding_hosted_inference_grants_evaluation_id UNIQUE (evaluation_id)
    );
    -- next --
    CREATE TABLE coding_hosted_inference_requests (
    request_id UUID NOT NULL,
    grant_id UUID NOT NULL,
    sequence INTEGER NOT NULL,
    locked_request_sha256 TEXT NOT NULL,
    prompt_ceiling BIGINT NOT NULL,
    completion_ceiling BIGINT NOT NULL,
    cost_ceiling BIGINT NOT NULL,
    state TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    finalized_at TIMESTAMP WITH TIME ZONE,
    prompt_tokens BIGINT,
    completion_tokens BIGINT,
    cost_usd_micros BIGINT,
    settlement_sha256 TEXT,
    provider_receipt_sha256 TEXT,
    settlement JSONB,
    CONSTRAINT pk_coding_hosted_inference_requests PRIMARY KEY (request_id),
    CONSTRAINT fk_coding_hosted_inference_requests_grant_id_coding_hos_c7b4 FOREIGN
      KEY(grant_id) REFERENCES coding_hosted_inference_grants (grant_id) ON DELETE
      RESTRICT,
    CONSTRAINT uq_coding_hosted_inference_requests_grant_id UNIQUE (grant_id,
      sequence),
    CONSTRAINT ck_coding_hosted_inference_requests_hosted_inference_re_1c18 CHECK
      (sequence BETWEEN 1 AND 256 AND locked_request_sha256 ~ '^[0-9a-f]{64}$' AND
      prompt_ceiling BETWEEN 1 AND 2250000 AND completion_ceiling BETWEEN 1 AND 250000
      AND cost_ceiling BETWEEN 1 AND 100000000),
    CONSTRAINT ck_coding_hosted_inference_requests_hosted_inference_re_0797 CHECK
      (((state= 'reserved' AND finalized_at IS NULL) OR (state= 'uncertain' AND
      finalized_at IS NOT NULL AND finalized_at>=created_at)) AND prompt_tokens IS NULL
      AND completion_tokens IS NULL AND cost_usd_micros IS NULL AND settlement_sha256
      IS NULL AND provider_receipt_sha256 IS NULL AND settlement IS NULL OR (state=
      'settled' AND finalized_at IS NOT NULL AND finalized_at>=created_at AND
      prompt_tokens IS NOT NULL AND prompt_tokens BETWEEN 0 AND prompt_ceiling AND
      completion_tokens IS NOT NULL AND completion_tokens BETWEEN 0 AND
      completion_ceiling AND cost_usd_micros IS NOT NULL AND cost_usd_micros BETWEEN 0
      AND cost_ceiling AND settlement_sha256 IS NOT NULL AND settlement_sha256 ~
      '^[0-9a-f]{64}$' AND provider_receipt_sha256 IS NOT NULL AND
      provider_receipt_sha256 ~ '^[0-9a-f]{64}$' AND settlement IS NOT NULL AND
      jsonb_typeof(settlement)= 'object' AND octet_length(settlement::text)<=8192)),
    CONSTRAINT uq_coding_hosted_inference_requests_provider_receipt_sha256 UNIQUE
      (provider_receipt_sha256)
    );
    -- next --
    CREATE FUNCTION coding_hosted_inference_grant_guard() RETURNS trigger AS $$
    DECLARE a coding_hosted_assignments%ROWTYPE;
    BEGIN
      IF TG_OP= 'DELETE' THEN RAISE EXCEPTION 'hosted inference grant is immutable'
        USING ERRCODE= '23514' ; END IF;
      IF TG_OP='UPDATE' THEN
        IF (to_jsonb(NEW)-'revoked_at') IS DISTINCT FROM (to_jsonb(OLD)-'revoked_at')
           OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM
             OLD.revoked_at)
           OR NEW.revoked_at IS NULL THEN
          RAISE EXCEPTION 'hosted inference grant cannot change or reopen' USING
            ERRCODE= '23514' ;
        END IF;
      ELSE
        SELECT * INTO a FROM coding_hosted_assignments WHERE
          evaluation_id=NEW.evaluation_id;
        IF NOT FOUND OR a.started_at IS NULL OR a.attempt_id<>NEW.attempt_id OR
          a.worker_id<>NEW.worker_id
           OR a.assignment_sha256<>NEW.assignment_sha256
           OR (a.authority->>'policy_sha256') IS DISTINCT FROM NEW.policy_sha256
           OR (a.authority->>'execution_profile_sha256')
             IS DISTINCT FROM NEW.execution_profile_sha256
           OR NEW.expires_at>a.expires_at OR NEW.created_at<a.started_at OR
             NEW.created_at>clock_timestamp()
           OR NEW.revoked_at IS NOT NULL OR a.expires_at<=clock_timestamp()
           OR NOT EXISTS(SELECT 1 FROM coding_hosted_private_tasks t WHERE
             t.evaluation_id=NEW.evaluation_id AND t.closed_at IS NULL AND t.frozen_at
             IS NULL)
        THEN RAISE EXCEPTION 'hosted inference grant lacks authoring authority' USING
          ERRCODE= '23514' ; END IF;
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql;
    -- next --
    CREATE TRIGGER coding_hosted_inference_grant_guard BEFORE INSERT OR UPDATE OR
      DELETE ON coding_hosted_inference_grants FOR EACH ROW EXECUTE FUNCTION
      coding_hosted_inference_grant_guard();

    -- next --
    CREATE FUNCTION coding_hosted_inference_request_guard() RETURNS trigger AS $$
    DECLARE g coding_hosted_inference_grants%ROWTYPE; used_prompt bigint;
      used_completion bigint; used_cost bigint; next_sequence integer;
      mutable text[]:=ARRAY[ 'state' , 'finalized_at' , 'prompt_tokens' ,
        'completion_tokens' , 'cost_usd_micros' , 'settlement_sha256' ,
        'provider_receipt_sha256' , 'settlement' ];
    BEGIN
      IF TG_OP= 'DELETE' THEN RAISE EXCEPTION
        'hosted inference requests cannot be deleted' USING ERRCODE= '23514' ; END IF;
      IF TG_OP='UPDATE' THEN
        IF (to_jsonb(NEW)-mutable) IS DISTINCT FROM (to_jsonb(OLD)-mutable)
           OR OLD.state<>'reserved' OR NEW.state NOT IN ('settled','uncertain') THEN
          RAISE EXCEPTION 'hosted inference request is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.state='uncertain' THEN
          UPDATE coding_hosted_inference_grants SET
            revoked_at=COALESCE(revoked_at,clock_timestamp()) WHERE
            grant_id=NEW.grant_id;
        END IF;
      ELSE
        SELECT * INTO g FROM coding_hosted_inference_grants WHERE grant_id=NEW.grant_id
          FOR UPDATE;
        IF NOT FOUND OR g.revoked_at IS NOT NULL OR g.expires_at<=clock_timestamp() OR
          NEW.state<> 'reserved'
           OR NEW.created_at<g.created_at OR NEW.created_at>clock_timestamp()
           OR EXISTS(SELECT 1 FROM coding_hosted_inference_requests WHERE
             grant_id=NEW.grant_id AND state= 'reserved' ) THEN
          RAISE EXCEPTION 'hosted inference dispatch is unavailable' USING ERRCODE=
            '23514' ;
        END IF;
        SELECT COALESCE(MAX(sequence),0)+1,
          COALESCE(SUM(CASE WHEN state= 'settled' THEN prompt_tokens ELSE
            prompt_ceiling END),0),
          COALESCE(SUM(CASE WHEN state= 'settled' THEN completion_tokens ELSE
            completion_ceiling END),0),
          COALESCE(SUM(CASE WHEN state= 'settled' THEN cost_usd_micros ELSE
            cost_ceiling END),0)
        INTO next_sequence,used_prompt,used_completion,used_cost FROM
          coding_hosted_inference_requests WHERE grant_id=NEW.grant_id;
        IF NEW.sequence<>next_sequence OR NEW.sequence>g.request_limit OR
          used_prompt+NEW.prompt_ceiling>g.prompt_limit
           OR used_completion+NEW.completion_ceiling>g.completion_limit OR
             used_cost+NEW.cost_ceiling>g.cost_limit THEN
          RAISE EXCEPTION 'hosted inference budget exhausted' USING ERRCODE='23514';
        END IF;
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql;
    -- next --
    CREATE TRIGGER coding_hosted_inference_request_guard BEFORE INSERT OR UPDATE OR
      DELETE ON coding_hosted_inference_requests FOR EACH ROW EXECUTE FUNCTION
      coding_hosted_inference_request_guard();

    -- next --
    CREATE FUNCTION coding_hosted_inference_phase_guard() RETURNS trigger AS $$
    DECLARE g coding_hosted_inference_grants%ROWTYPE;
    BEGIN
      SELECT * INTO g FROM coding_hosted_inference_grants WHERE
        evaluation_id=NEW.evaluation_id FOR UPDATE;
      IF FOUND THEN
        IF OLD.frozen_at IS NULL AND NEW.frozen_at IS NOT NULL AND
          (g.revoked_at IS NULL OR EXISTS(SELECT 1 FROM
            coding_hosted_inference_requests WHERE grant_id=g.grant_id AND state=
            'reserved' )) THEN
          RAISE EXCEPTION 'hosted inference must be revoked and drained before freeze'
            USING ERRCODE= '23514' ;
        END IF;
        IF NEW.closed_at IS NOT NULL AND g.revoked_at IS NULL THEN
          UPDATE coding_hosted_inference_grants SET revoked_at=clock_timestamp() WHERE
            grant_id=g.grant_id;
        END IF;
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql;
    -- next --
    CREATE TRIGGER coding_hosted_inference_phase_guard BEFORE UPDATE ON
      coding_hosted_private_tasks FOR EACH ROW EXECUTE FUNCTION
      coding_hosted_inference_phase_guard();
    """
    for statement in statements.split("-- next --"):
        op.execute(statement)


def downgrade() -> None:
    statements = """
    DROP TRIGGER coding_hosted_inference_phase_guard ON coding_hosted_private_tasks;
    DROP FUNCTION coding_hosted_inference_phase_guard();
    DROP TABLE coding_hosted_inference_requests;
    DROP FUNCTION coding_hosted_inference_request_guard();
    DROP TABLE coding_hosted_inference_grants;
    DROP FUNCTION coding_hosted_inference_grant_guard();
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)
