/*
 session_user, because inside a definer function current_user is the owner, and SET ROLE or
 set_config('role', ...) only move current_user. A login with no principals row gets NULL, and
 every policy below then matches nothing.
*/
CREATE FUNCTION app.visible_regions() RETURNS text[] LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS
$$ SELECT regions FROM app.principals WHERE role_name = session_user $$;

ALTER FUNCTION app.visible_regions() OWNER TO claims_owner;
REVOKE EXECUTE ON FUNCTION app.visible_regions() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.visible_regions() TO chat_adjuster, chat_supervisor, chat_analyst;

DO $$
DECLARE
    tbl regclass;
BEGIN
    FOR tbl IN
        SELECT c.oid::regclass
        FROM pg_catalog.pg_class c
        WHERE c.relnamespace IN ('core'::regnamespace, 'rag'::regnamespace)
          AND c.relkind = 'r'
        UNION ALL
        SELECT 'app.principals'::regclass
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tbl);
    END LOOP;
END
$$;

/* The function owner reads principals through the definer function, one row at a time. */
CREATE POLICY principals_self ON app.principals FOR SELECT TO claims_owner
    USING (role_name = session_user);

/* The subquery around the helper makes it an initplan, evaluated once per statement. */
CREATE POLICY regions_visible ON core.regions FOR SELECT TO chat_adjuster, chat_supervisor
    USING (region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY states_visible ON core.states FOR SELECT TO chat_adjuster, chat_supervisor
    USING (region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY adjusters_visible ON core.adjusters FOR SELECT TO chat_adjuster, chat_supervisor
    USING (region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY policies_visible ON core.policies FOR SELECT TO chat_adjuster, chat_supervisor
    USING (region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY claims_visible ON core.claims FOR SELECT TO chat_adjuster, chat_supervisor
    USING (region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY payments_visible ON core.payments FOR SELECT TO chat_adjuster, chat_supervisor
    USING (region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY earned_premium_visible ON core.earned_premium FOR SELECT TO chat_adjuster, chat_supervisor
    USING (region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY policyholders_visible ON core.policyholders FOR SELECT TO chat_adjuster, chat_supervisor
    USING (state IN (SELECT s.state FROM core.states s WHERE s.region = ANY ((SELECT app.visible_regions())::text[])));

CREATE POLICY documents_general ON rag.documents FOR SELECT TO chat_adjuster, chat_supervisor, chat_analyst
    USING (sensitivity = 'general');
CREATE POLICY documents_claim ON rag.documents FOR SELECT TO chat_adjuster, chat_supervisor
    USING (sensitivity = 'claim' AND region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY chunks_general ON rag.chunks FOR SELECT TO chat_adjuster, chat_supervisor, chat_analyst
    USING (sensitivity = 'general');
CREATE POLICY chunks_claim ON rag.chunks FOR SELECT TO chat_adjuster, chat_supervisor
    USING (sensitivity = 'claim' AND region = ANY ((SELECT app.visible_regions())::text[]));
CREATE POLICY scan_fields_general ON rag.scan_fields FOR SELECT TO chat_adjuster, chat_supervisor, chat_analyst
    USING (sensitivity = 'general');
CREATE POLICY scan_fields_claim ON rag.scan_fields FOR SELECT TO chat_adjuster, chat_supervisor
    USING (sensitivity = 'claim' AND region = ANY ((SELECT app.visible_regions())::text[]));

/* agg.metric runs as agg_owner and aggregates every region; suppression is what protects the analyst. */
CREATE POLICY claims_aggregate ON core.claims FOR SELECT TO agg_owner USING (true);
CREATE POLICY payments_aggregate ON core.payments FOR SELECT TO agg_owner USING (true);
CREATE POLICY earned_premium_aggregate ON core.earned_premium FOR SELECT TO agg_owner USING (true);
