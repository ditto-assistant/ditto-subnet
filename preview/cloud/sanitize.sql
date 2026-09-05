-- Runs only inside the disposable sanitizer Postgres. The resulting dump is
-- useful for lifecycle/UI testing but contains no credentials, source-review
-- evidence, payments, request bodies, bearer digests, signatures, or operator
-- identity. Keep this policy explicit and fail closed in sanitize-snapshot.sh.
SET session_replication_role = replica;

CREATE TEMP TABLE preview_excluded_tables (
  table_name text PRIMARY KEY
);
\copy preview_excluded_tables (table_name) FROM '/sanitize-excluded-tables.txt'

DO $policy$
DECLARE
  target_table text;
  column_record record;
BEGIN
  FOR target_table IN
    SELECT excluded.table_name FROM preview_excluded_tables AS excluded
  LOOP
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
