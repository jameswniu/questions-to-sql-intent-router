CREATE ROLE claims_owner NOLOGIN;
CREATE ROLE agg_owner NOLOGIN;
CREATE ROLE chat_adjuster NOLOGIN;
CREATE ROLE chat_supervisor NOLOGIN;
CREATE ROLE chat_analyst NOLOGIN;

/*
 One login role per job and region, named in data/users.yaml. SET FALSE keeps a member from
 assuming the group itself, and bootstrap sets each password from var/secrets.env.
*/
CREATE ROLE u_adj_north LOGIN CONNECTION LIMIT 4;
CREATE ROLE u_adj_south LOGIN CONNECTION LIMIT 4;
CREATE ROLE u_adj_east LOGIN CONNECTION LIMIT 4;
CREATE ROLE u_adj_west LOGIN CONNECTION LIMIT 4;
CREATE ROLE u_supervisor LOGIN CONNECTION LIMIT 4;
CREATE ROLE u_analyst LOGIN CONNECTION LIMIT 4;

GRANT chat_adjuster TO u_adj_north, u_adj_south, u_adj_east, u_adj_west WITH INHERIT TRUE, SET FALSE;
GRANT chat_supervisor TO u_supervisor WITH INHERIT TRUE, SET FALSE;
GRANT chat_analyst TO u_analyst WITH INHERIT TRUE, SET FALSE;

DO $$
DECLARE
    login text;
BEGIN
    FOREACH login IN ARRAY ARRAY['u_adj_north', 'u_adj_south', 'u_adj_east', 'u_adj_west', 'u_supervisor', 'u_analyst']
    LOOP
        EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', login);
        EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', login, '4s');
        EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', login, '10s');
    END LOOP;
END
$$;

CREATE ROLE app_writer LOGIN CONNECTION LIMIT 8;
CREATE ROLE gold_reader LOGIN BYPASSRLS CONNECTION LIMIT 4;

DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO chat_adjuster, chat_supervisor, chat_analyst, app_writer, gold_reader',
        current_database()
    );
END
$$;
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

/*
 Built-ins that reach past the table grants, revoked by name so every overload goes. No role
 needs any of them to answer a question.
   set_config moves current_user without SET ROLE; pg_sleep holds a connection on purpose.
   The *_to_xml family, ts_stat and ts_rewrite run SQL handed to them as a string.
   Large objects are a write path that no grant on a table covers.
   Advisory locks survive a rolled-back statement on a pooled connection, and enough of them
   fill the shared lock table.
   pg_logical_emit_message writes WAL, pg_notify fills the shared notification queue, and
   pg_export_snapshot holds back vacuum for as long as the snapshot is kept.
   txid_current and pg_current_xact_id assign a transaction id each call, which a loop can burn.
   pg_cancel_backend and pg_terminate_backend reach the other pooled connections of the same role.
   Replication slot functions pin WAL until the disk fills. They also require the REPLICATION
   attribute; this makes that check not the only one.
   pg_stat_reset* are already revoked by default, and are named so it stays that way.
 Left alone after surveying the volatile PUBLIC functions: those that only read, those whose
 state is local to the session, and those that check ownership or superuser themselves
 (brin_summarize_*, gin_clean_pending_list, nextval and setval, binary_upgrade_*).
*/
DO $$
DECLARE
    fn regprocedure;
BEGIN
    FOR fn IN
        SELECT p.oid::regprocedure
        FROM pg_catalog.pg_proc p
        WHERE p.pronamespace = 'pg_catalog'::regnamespace
          AND (
              p.proname IN (
                  'set_config', 'pg_sleep', 'pg_sleep_for', 'pg_sleep_until', 'loread', 'lowrite',
                  'ts_stat', 'ts_rewrite', 'pg_logical_emit_message', 'pg_notify', 'pg_export_snapshot',
                  'txid_current', 'txid_current_if_assigned', 'pg_current_xact_id', 'pg_current_xact_id_if_assigned',
                  'pg_cancel_backend', 'pg_terminate_backend'
              )
              OR p.proname ~ '^(query|table|cursor|schema|database)_to_xml'
              OR p.proname ~ '^lo_'
              OR p.proname ~ '^pg_(try_)?advisory_'
              OR p.proname ~ '^pg_stat_reset'
              OR p.proname ~ '(replication_slot|^pg_logical_slot_)'
          )
          AND has_function_privilege('public', p.oid, 'EXECUTE')
    LOOP
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', fn);
    END LOOP;
END
$$;

DO $$
DECLARE
    tbl regclass;
BEGIN
    FOR tbl IN
        SELECT c.oid::regclass
        FROM pg_catalog.pg_class c
        WHERE c.relnamespace IN ('core'::regnamespace, 'rag'::regnamespace, 'app'::regnamespace)
          AND c.relkind = 'r'
    LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO claims_owner', tbl);
    END LOOP;
END
$$;

ALTER FUNCTION rag.row_matches_document() OWNER TO claims_owner;
ALTER FUNCTION rag.document_matches_rows() OWNER TO claims_owner;

/* Foreign-key checks and the definer function run as the owner, which needs to reach the schemas. */
GRANT USAGE ON SCHEMA core, rag, app TO claims_owner;

GRANT USAGE ON SCHEMA core, sem, rag TO chat_adjuster, chat_supervisor;
GRANT SELECT ON core.claims, core.payments, core.earned_premium, core.policies, core.states, core.regions,
    core.adjusters TO chat_adjuster, chat_supervisor;
GRANT SELECT (policyholder_id, first_name, last_name, state) ON core.policyholders TO chat_adjuster, chat_supervisor;
GRANT SELECT ON rag.documents, rag.chunks, rag.scan_fields TO chat_adjuster, chat_supervisor;

GRANT USAGE ON SCHEMA agg, rag TO chat_analyst;
GRANT SELECT ON rag.documents, rag.chunks, rag.scan_fields TO chat_analyst;

GRANT USAGE ON SCHEMA app TO chat_adjuster, chat_supervisor, chat_analyst;

GRANT USAGE ON SCHEMA core, app TO agg_owner;
GRANT SELECT ON core.claims, core.payments, core.earned_premium TO agg_owner;
GRANT SELECT ON app.settings TO agg_owner;

GRANT USAGE ON SCHEMA ops TO app_writer;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA ops TO app_writer;

GRANT USAGE ON SCHEMA core TO gold_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO gold_reader;
