-- Runs only inside the disposable sanitizer Postgres. The resulting dump is
-- useful for lifecycle/UI testing but contains no credentials, source-review
-- evidence, payments, request bodies, bearer digests, signatures, or operator
-- identity. Keep this policy explicit and fail closed in sanitize-snapshot.sh.
SET session_replication_role = replica;

DO $policy$
DECLARE
  target_table text;
  column_record record;
BEGIN
  FOREACH target_table IN ARRAY ARRAY[
    'artifact_fetch_audit',
    'banned_hotkeys',
    'confirmation_dimension_evidence',
    'confirmation_inference_grants',
    'confirmation_inference_requests',
    'evaluation_payments',
    'hotkey_ban_audit',
    'inference_grants',
    'inference_requests',
    'inference_routing_audit',
    'miner_avatar_nonces',
    'miner_avatars',
    'miner_device_grants',
    'miner_login_nonces',
    'miner_oauth_clients',
    'miner_oauth_codes',
    'miner_profiles',
    'miner_session_tokens',
    'miner_sessions',
    'name_claim_endorsements',
    'name_claims',
    'owner_attestations',
    'screened_image_uploads',
    'screener_heartbeats',
    'screener_node_bootstrap_grants',
    'screener_nodes',
    'screener_shadow_reviews',
    'screening_disputes',
    'screening_quarantine_resolutions',
    'screening_quarantines',
    'screening_retry_overrides',
    'submission_image_builds',
    'submission_deposit_address_revisions',
    'submission_source_reviews',
    'trusted_image_builds',
    'upload_admission_reservations',
    'validator_heartbeats',
    'validator_request_nonces'
  ] LOOP
    IF to_regclass('public.' || target_table) IS NOT NULL THEN
      EXECUTE format('TRUNCATE TABLE %I CASCADE', target_table);
    END IF;
  END LOOP;

  -- Redact every remaining high-risk column by name. Unknown future columns
  -- matching these classes are scrubbed automatically; unsupported non-null
  -- types fail the sanitizer instead of publishing a partially scrubbed dump.
  FOR column_record IN
    SELECT table_name, column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND (
        column_name ~* '((^|_)token(_hash)?$|secret|signature|bearer|password|cookie|nonce|code_challenge|private|log_tail$|evidence|fingerprint|object_key|image_ref|source_repository|provider_resource_id|payment_address|dest_address|coldkey)'
        OR column_name IN ('details', 'detail', 'payload', 'finding', 'observation', 'message')
      )
  LOOP
    IF column_record.is_nullable = 'YES' THEN
      EXECUTE format('UPDATE %I SET %I = NULL', column_record.table_name, column_record.column_name);
    ELSIF column_record.data_type IN ('json', 'jsonb') THEN
      EXECUTE format('UPDATE %I SET %I = ''{}''::%s', column_record.table_name, column_record.column_name, column_record.data_type);
    ELSIF column_record.data_type IN ('text', 'character varying', 'character') THEN
      EXECUTE format(
        'UPDATE %I SET %I = repeat(''0'', char_length(%I))',
        column_record.table_name, column_record.column_name, column_record.column_name
      );
    ELSIF column_record.data_type = 'bytea' THEN
      EXECUTE format('UPDATE %I SET %I = decode('''', ''hex'')', column_record.table_name, column_record.column_name);
    ELSIF column_record.data_type = 'uuid' THEN
      EXECUTE format('UPDATE %I SET %I = gen_random_uuid()', column_record.table_name, column_record.column_name);
    ELSE
      RAISE EXCEPTION 'sanitizer has no safe replacement for %.% (%)', column_record.table_name, column_record.column_name, column_record.data_type;
    END IF;
  END LOOP;

  -- Operator identities can appear in ordinary actor/reason columns.
  FOR column_record IN
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = 'public' AND column_name IN ('actor', 'resolved_by', 'created_by', 'requester_id')
  LOOP
    EXECUTE format('UPDATE %I SET %I = ''preview-sanitizer'' WHERE %I IS NOT NULL', column_record.table_name, column_record.column_name, column_record.column_name);
  END LOOP;

  -- Preserve relational hotkey joins while making every key irreversible and
  -- visibly synthetic. Triggers stay disabled until all referencing columns
  -- have moved through the same deterministic transform.
  FOR column_record IN
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = 'public' AND column_name ~* 'hotkey$'
  LOOP
    EXECUTE format(
      'UPDATE %I SET %I = ''preview-'' || substr(encode(digest(%I, ''sha256''), ''hex''), 1, 32) WHERE %I IS NOT NULL',
      column_record.table_name, column_record.column_name, column_record.column_name, column_record.column_name
    );
  END LOOP;
END
$policy$;

SET session_replication_role = DEFAULT;
VACUUM (ANALYZE);
