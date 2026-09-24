/*
 The views the SQL generator is allowed to name. security_invoker makes every read inside them
 happen as the asking role, so the base-table grants and policies still decide what comes back.
*/
CREATE VIEW sem.v_claims WITH (security_invoker = true) AS
SELECT
    c.claim_id,
    c.region,
    c.state,
    c.peril,
    c.loss_date,
    c.reported_date,
    c.closed_date,
    c.status,
    c.channel,
    c.adjuster_id,
    c.edition,
    c.deductible,
    c.reserve,
    c.denial_reason,
    coalesce(paid.paid_total, 0) AS paid_total
FROM core.claims c
LEFT JOIN (
    SELECT p.claim_id, sum(p.amount) AS paid_total
    FROM core.payments p
    WHERE p.status <> 'voided'
    GROUP BY p.claim_id
) paid ON paid.claim_id = c.claim_id;

CREATE VIEW sem.v_payments_net WITH (security_invoker = true) AS
SELECT
    p.payment_id,
    p.claim_id,
    p.region,
    c.state,
    c.peril,
    p.paid_date,
    p.amount,
    p.kind
FROM core.payments p
JOIN core.claims c ON c.claim_id = p.claim_id
WHERE p.status <> 'voided';

CREATE VIEW sem.v_premium WITH (security_invoker = true) AS
SELECT
    e.region,
    e.month,
    e.amount AS earned_premium,
    e.exposure
FROM core.earned_premium e;

CREATE VIEW sem.v_claim_detail WITH (security_invoker = true) AS
SELECT
    c.claim_id,
    c.policy_id,
    c.region,
    c.state,
    c.peril,
    c.loss_date,
    c.reported_date,
    c.closed_date,
    c.status,
    c.channel,
    c.adjuster_id,
    a.name AS adjuster_name,
    c.edition,
    c.deductible,
    c.reserve,
    c.damage_estimate,
    c.denial_reason,
    h.first_name,
    h.last_name,
    pol.coverage_a,
    pol.original_effective,
    v.paid_total
FROM core.claims c
JOIN sem.v_claims v ON v.claim_id = c.claim_id
JOIN core.policies pol ON pol.policy_id = c.policy_id
JOIN core.policyholders h ON h.policyholder_id = pol.policyholder_id
JOIN core.adjusters a ON a.adjuster_id = c.adjuster_id;

ALTER VIEW sem.v_claims OWNER TO claims_owner;
ALTER VIEW sem.v_payments_net OWNER TO claims_owner;
ALTER VIEW sem.v_premium OWNER TO claims_owner;
ALTER VIEW sem.v_claim_detail OWNER TO claims_owner;

GRANT SELECT ON ALL TABLES IN SCHEMA sem TO chat_adjuster, chat_supervisor;
