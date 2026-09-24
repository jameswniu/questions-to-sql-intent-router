/*
 The analyst never reads a row. agg.metric aggregates as agg_owner over a whitelist of measures
 and dimensions, and withholds any cell that is too small or that one claim dominates.
*/
GRANT USAGE ON SCHEMA agg TO agg_owner;

CREATE VIEW agg.f_claims WITH (security_invoker = true) AS
SELECT
    c.claim_id,
    c.region,
    c.state,
    c.peril,
    c.channel,
    c.loss_date,
    c.closed_date,
    c.status,
    coalesce(paid.amount, 0) AS paid_total
FROM core.claims c
LEFT JOIN (
    SELECT p.claim_id, sum(p.amount) AS amount
    FROM core.payments p
    WHERE p.status <> 'voided'
    GROUP BY p.claim_id
) paid ON paid.claim_id = c.claim_id;

CREATE VIEW agg.f_payments WITH (security_invoker = true) AS
SELECT
    p.claim_id,
    p.region,
    c.state,
    c.peril,
    c.channel,
    p.paid_date,
    p.amount
FROM core.payments p
JOIN core.claims c ON c.claim_id = p.claim_id
WHERE p.status <> 'voided';

CREATE VIEW agg.f_premium WITH (security_invoker = true) AS
SELECT e.region, e.month, e.amount AS earned_premium
FROM core.earned_premium e;

/*
 source and den_source name views in agg. The expressions are SQL fragments, trusted because only
 agg_owner and the superuser can write these tables. A measure may be grouped or filtered only by
 the dimensions listed in its dims column.
*/
CREATE TABLE agg.allowed_measures (
    name text PRIMARY KEY,
    source text NOT NULL,
    date_col text NOT NULL,
    row_filter text,
    num_expr text NOT NULL,
    den_expr text,
    den_source text,
    den_date_col text,
    dominance_expr text,
    dims text[] NOT NULL,
    CHECK ((den_source IS NULL) = (den_date_col IS NULL)),
    CHECK (den_source IS NULL OR den_expr IS NOT NULL)
);

/*
 %1$I is replaced by the measure's date column, so month means loss month for claim_count and paid
 month for paid_losses. No time dimension is finer than a month, for the reason agg.metric gives
 for its period bounds.
*/
CREATE TABLE agg.allowed_dims (
    name text PRIMARY KEY,
    expr text NOT NULL
);

INSERT INTO agg.allowed_dims (name, expr) VALUES
    ('region', 'region'),
    ('state', 'state'),
    ('peril', 'peril'),
    ('channel', 'channel'),
    ('month', 'to_char(%1$I, ''YYYY-MM'')'),
    ('quarter', 'to_char(%1$I, ''YYYY-"Q"Q'')'),
    ('year', 'to_char(%1$I, ''YYYY'')');

INSERT INTO agg.allowed_measures
    (name, source, date_col, row_filter, num_expr, den_expr, den_source, den_date_col, dominance_expr, dims)
VALUES
    ('claim_count', 'f_claims', 'loss_date', NULL,
        'count(*)', NULL, NULL, NULL, NULL,
        '{region,state,peril,channel,month,quarter,year}'),
    ('paid_losses', 'f_payments', 'paid_date', NULL,
        'sum(amount)', NULL, NULL, NULL, 'amount',
        '{region,state,peril,channel,month,quarter,year}'),
    ('claims_paid', 'f_payments', 'paid_date', NULL,
        'count(DISTINCT claim_id)', NULL, NULL, NULL, NULL,
        '{region,state,peril,channel,month,quarter,year}'),
    ('avg_severity', 'f_claims', 'closed_date', 'status = ''closed'' AND paid_total > 0',
        'sum(paid_total)', 'count(*)', NULL, NULL, 'paid_total',
        '{region,state,peril,channel,month,quarter,year}'),
    ('denial_rate', 'f_claims', 'closed_date', 'status IN (''closed'', ''denied'')',
        'count(*) FILTER (WHERE status = ''denied'')', 'count(*)', NULL, NULL, NULL,
        '{region,state,peril,channel,month,quarter,year}'),
    ('loss_ratio', 'f_payments', 'paid_date', NULL,
        'sum(amount)', 'sum(earned_premium)', 'f_premium', 'month', 'amount',
        '{region,month,quarter,year}');

/*
 Both period bounds are inclusive and either may be NULL. A period starts on the first day of a
 month and ends on the last day of one: with day-level bounds, two cumulative queries a day apart
 differ by a single claim's payment, and both can pass suppression. filters maps a dimension to
 the values it may take, e.g. {"region": ["West"], "peril": ["hail", "wind"]}. Every value the
 caller supplies reaches the query as a bound parameter; only whitelisted names are ever formatted
 into it. A suppressed cell comes back with num, den and n all NULL, since n alone is the answer
 for a count.

 Known gap: a suppressed cell can still be recovered as the difference of two published ones that
 overlap in everything else, such as a region total minus its other states, or a range through
 June minus the same range through May. Closing that needs cross-query auditing or noise.
*/
CREATE FUNCTION agg.metric(
    measure text,
    group_by text[] DEFAULT '{}',
    filters jsonb DEFAULT '{}',
    period_start date DEFAULT NULL,
    period_end date DEFAULT NULL
) RETURNS TABLE (grp jsonb, num numeric, den numeric, n bigint, suppressed boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS
$fn$
DECLARE
    m agg.allowed_measures;
    min_cells int;
    max_share numeric;
    dim text;
    dim_expr text;
    filter_key text;
    filter_values jsonb;
    g text;
    seen text[] := '{}';
    g_cols text[] := '{}';
    grp_pairs text[] := '{}';
    num_select text[] := '{}';
    den_select text[] := '{}';
    num_where text[];
    den_where text[];
    group_clause text := '';
    den_clause text;
    query text;
BEGIN
    SELECT am.* INTO m FROM agg.allowed_measures am WHERE am.name = metric.measure;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown measure: %', metric.measure USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT s.value::int INTO min_cells FROM app.settings s WHERE s.key = 'min_cell_count';
    SELECT s.value::numeric INTO max_share FROM app.settings s WHERE s.key = 'dominance_share';
    IF min_cells IS NULL OR max_share IS NULL THEN
        RAISE EXCEPTION 'suppression settings are missing' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(coalesce(metric.filters, '{}')) <> 'object' THEN
        RAISE EXCEPTION 'filters must be a JSON object' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF metric.period_start > metric.period_end THEN
        RAISE EXCEPTION 'period_start is after period_end' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF extract(day FROM metric.period_start) <> 1 THEN
        RAISE EXCEPTION 'period_start must be the first day of a month, not %', metric.period_start
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF extract(day FROM metric.period_end + 1) <> 1 THEN
        RAISE EXCEPTION 'period_end must be the last day of a month, not %', metric.period_end
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    num_where := ARRAY[format('($2::date IS NULL OR %1$I >= $2) AND ($3::date IS NULL OR %1$I <= $3)', m.date_col)];
    IF m.den_source IS NOT NULL THEN
        den_where := ARRAY[format('($2::date IS NULL OR %1$I >= $2) AND ($3::date IS NULL OR %1$I <= $3)', m.den_date_col)];
    END IF;
    IF m.row_filter IS NOT NULL THEN
        num_where := num_where || format('(%s)', m.row_filter);
    END IF;

    FOREACH dim IN ARRAY coalesce(metric.group_by, '{}') LOOP
        SELECT d.expr INTO dim_expr FROM agg.allowed_dims d WHERE d.name = dim AND d.name = ANY (m.dims);
        IF NOT FOUND THEN
            RAISE EXCEPTION 'unknown dimension for %: %', m.name, dim USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF dim = ANY (seen) THEN
            RAISE EXCEPTION 'dimension named twice: %', dim USING ERRCODE = 'invalid_parameter_value';
        END IF;
        seen := seen || dim;
        g := 'g' || cardinality(seen);
        g_cols := g_cols || g;
        grp_pairs := grp_pairs || format('%L, c.%I', dim, g);
        num_select := num_select || format('(%s) AS %I', format(dim_expr, m.date_col), g);
        IF m.den_source IS NOT NULL THEN
            den_select := den_select || format('(%s) AS %I', format(dim_expr, m.den_date_col), g);
        END IF;
    END LOOP;

    FOR filter_key, filter_values IN SELECT f.key, f.value FROM jsonb_each(coalesce(metric.filters, '{}')) f LOOP
        SELECT d.expr INTO dim_expr FROM agg.allowed_dims d WHERE d.name = filter_key AND d.name = ANY (m.dims);
        IF NOT FOUND THEN
            RAISE EXCEPTION 'unknown dimension for %: %', m.name, filter_key USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF jsonb_typeof(filter_values) <> 'array' THEN
            RAISE EXCEPTION 'filter % must be a list of values', filter_key USING ERRCODE = 'invalid_parameter_value';
        END IF;
        num_where := num_where || format('(%s) IN (SELECT jsonb_array_elements_text($1 -> %L))',
            format(dim_expr, m.date_col), filter_key);
        IF m.den_source IS NOT NULL THEN
            den_where := den_where || format('(%s) IN (SELECT jsonb_array_elements_text($1 -> %L))',
                format(dim_expr, m.den_date_col), filter_key);
        END IF;
    END LOOP;

    IF cardinality(g_cols) > 0 THEN
        group_clause := ' GROUP BY ' || array_to_string(g_cols, ', ');
    END IF;

    query := format(
        'WITH base AS (SELECT f.* %s FROM agg.%I f WHERE %s), '
        'shares AS (SELECT base.*, sum(%s) OVER (PARTITION BY %s) AS claim_share FROM base), '
        'cells AS (SELECT %s (%s)::numeric AS num, (%s)::numeric AS den, count(DISTINCT claim_id) AS n, '
        'max(claim_share) AS top FROM shares%s)',
        (SELECT string_agg(', ' || s, '') FROM unnest(num_select) s),
        m.source,
        array_to_string(num_where, ' AND '),
        coalesce(m.dominance_expr, 'NULL::numeric'),
        array_to_string(g_cols || 'claim_id'::text, ', '),
        (SELECT string_agg(s || ', ', '') FROM unnest(g_cols) s),
        m.num_expr,
        CASE WHEN m.den_source IS NULL THEN coalesce(m.den_expr, 'NULL') ELSE 'NULL' END,
        group_clause
    );

    IF m.den_source IS NOT NULL THEN
        query := query || format(
            ', den_cells AS (SELECT %s (%s)::numeric AS den FROM agg.%I f WHERE %s%s)',
            (SELECT string_agg(s || ', ', '') FROM unnest(den_select) s),
            m.den_expr,
            m.den_source,
            array_to_string(den_where, ' AND '),
            group_clause
        );
        den_clause := CASE WHEN cardinality(g_cols) > 0
            THEN ' LEFT JOIN den_cells d USING (' || array_to_string(g_cols, ', ') || ')'
            ELSE ' CROSS JOIN den_cells d' END;
    ELSE
        den_clause := '';
    END IF;

    query := query || format(
        ' SELECT grp, CASE WHEN hide THEN NULL ELSE num END, CASE WHEN hide THEN NULL ELSE den END,'
        ' CASE WHEN hide THEN NULL ELSE n END, hide'
        ' FROM (SELECT %s AS grp, c.num, %s AS den, c.n, %s'
        ' (c.n < $4 OR (c.top IS NOT NULL AND c.num > 0 AND c.top > $5 * c.num)) AS hide'
        ' FROM cells c%s) r%s',
        CASE WHEN cardinality(grp_pairs) > 0
            THEN 'jsonb_build_object(' || array_to_string(grp_pairs, ', ') || ')'
            ELSE '''{}''::jsonb' END,
        CASE WHEN m.den_source IS NULL THEN 'c.den' ELSE 'd.den' END,
        (SELECT string_agg(format('c.%I, ', s), '') FROM unnest(g_cols) s),
        den_clause,
        CASE WHEN cardinality(g_cols) > 0 THEN ' ORDER BY ' || array_to_string(g_cols, ', ') ELSE '' END
    );

    RETURN QUERY EXECUTE query USING metric.filters, metric.period_start, metric.period_end, min_cells, max_share;
END
$fn$;

ALTER TABLE agg.allowed_measures OWNER TO agg_owner;
ALTER TABLE agg.allowed_dims OWNER TO agg_owner;
ALTER VIEW agg.f_claims OWNER TO agg_owner;
ALTER VIEW agg.f_payments OWNER TO agg_owner;
ALTER VIEW agg.f_premium OWNER TO agg_owner;
ALTER FUNCTION agg.metric(text, text[], jsonb, date, date) OWNER TO agg_owner;
REVOKE EXECUTE ON FUNCTION agg.metric(text, text[], jsonb, date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agg.metric(text, text[], jsonb, date, date) TO chat_analyst;
