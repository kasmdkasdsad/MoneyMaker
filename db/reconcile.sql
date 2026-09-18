-- =============================================================================
-- Operational reconciliation queries.
--
-- Run on a schedule and alert on any non-empty result. These check the
-- invariants that the schema alone cannot: drift between denormalised counters
-- and their source of truth, money stuck in the wrong state, and stops that are
-- quietly losing money.
-- =============================================================================

-- 1. COUNTER DRIFT. campaigns.committed_units is the authority for threshold
--    decisions, so if it disagrees with the orders it summarises, every
--    threshold decision on that campaign is suspect. Should always be empty.
SELECT
    c.id AS campaign_id,
    c.state,
    c.committed_units AS counter,
    COALESCE(o.actual, 0) AS actual_from_orders,
    c.committed_units - COALESCE(o.actual, 0) AS drift
FROM campaigns c
LEFT JOIN (
    SELECT campaign_id, SUM(quantity) AS actual
    FROM orders
    WHERE state IN ('committed','locked','ready_for_pickup','picked_up')
    GROUP BY campaign_id
) o ON o.campaign_id = c.id
WHERE c.state NOT IN ('expired','cancelled')
  AND c.committed_units <> COALESCE(o.actual, 0);

-- 2. RESERVATION DRIFT. reserved_units should equal the units held by orders
--    still awaiting authorization.
SELECT
    c.id AS campaign_id,
    c.reserved_units AS counter,
    COALESCE(o.actual, 0) AS actual_pending
FROM campaigns c
LEFT JOIN (
    SELECT campaign_id, SUM(quantity) AS actual
    FROM orders WHERE state = 'pending_auth'
    GROUP BY campaign_id
) o ON o.campaign_id = c.id
WHERE c.state = 'open'
  AND c.reserved_units <> COALESCE(o.actual, 0);

-- 3. STRANDED RESERVATIONS. Capacity held by checkouts that never completed.
--    The sweeper should keep this empty; a growing count means the sweeper is
--    down and popular campaigns are turning away real buyers.
SELECT id, campaign_id, quantity, reserved_until, now() - reserved_until AS overdue_by
FROM orders
WHERE state = 'pending_auth' AND reserved_until < now()
ORDER BY reserved_until;

-- 4. AUTHORIZATIONS ABOUT TO DECAY. The single most dangerous state in the
--    system: a hold that expires before we capture is revenue we simply lose.
--    Campaign windows are capped at 5 days precisely to keep this empty.
SELECT
    pi.order_id,
    o.campaign_id,
    c.state AS campaign_state,
    pi.authorization_expires_at,
    pi.authorization_expires_at - now() AS expires_in,
    pi.authorized_amount_cents
FROM payment_intents pi
JOIN orders o ON o.id = pi.order_id
JOIN campaigns c ON c.id = o.campaign_id
WHERE pi.state = 'authorized'
  AND pi.authorization_expires_at < now() + interval '48 hours'
ORDER BY pi.authorization_expires_at;

-- 5. LOCKED BUT UNCAPTURED. A locked campaign has cut a supplier PO, so every
--    order under it must have money captured. Non-empty means we owe a supplier
--    for goods we have not been paid for.
SELECT o.id AS order_id, o.campaign_id, o.quantity, pi.state AS payment_state
FROM orders o
JOIN campaigns c ON c.id = o.campaign_id
LEFT JOIN payment_intents pi ON pi.order_id = o.id
WHERE c.state IN ('locked','purchasing','in_transit','ready_for_pickup')
  AND o.state IN ('locked','ready_for_pickup','picked_up')
  AND (pi.id IS NULL OR pi.state <> 'captured');

-- 6. OUTBOX HEALTH. Backlog age and messages that exhausted their retries.
--    An exhausted message is almost always a money failure needing a human.
SELECT
    count(*) FILTER (WHERE processed_at IS NULL)                      AS pending,
    count(*) FILTER (WHERE processed_at IS NULL AND attempts >= 8)    AS exhausted,
    max(now() - created_at) FILTER (WHERE processed_at IS NULL)       AS oldest_pending_age
FROM outbox;

SELECT id, aggregate_type, aggregate_id, event_type, attempts, last_error
FROM outbox
WHERE processed_at IS NULL AND attempts >= 8
ORDER BY id;

-- 7. LEDGER BALANCE. Double entry means every txn_id must sum to zero. A
--    non-zero group is a bookkeeping bug; find it before an auditor does.
SELECT txn_id, SUM(amount_cents) AS imbalance
FROM ledger_entries
GROUP BY txn_id
HAVING SUM(amount_cents) <> 0;

-- 8. LAST-MILE EFFICIENCY. Units delivered per stop, by run. This is the number
--    the entire go-to-market plan exists to raise; anything below the
--    neighbourhood's break-even stop density is a stop that lost money.
SELECT
    dr.id AS run_id,
    n.name AS neighborhood,
    dr.run_date,
    count(rs.id) AS stops,
    COALESCE(SUM(rs.delivered_units), SUM(rs.planned_units)) AS units,
    ROUND(
        COALESCE(SUM(rs.delivered_units), SUM(rs.planned_units))::numeric
        / NULLIF(count(rs.id), 0), 1
    ) AS units_per_stop,
    dr.total_cost_cents,
    ROUND(
        dr.total_cost_cents::numeric
        / NULLIF(COALESCE(SUM(rs.delivered_units), SUM(rs.planned_units)), 0), 1
    ) AS cost_per_unit_cents
FROM delivery_runs dr
JOIN neighborhoods n ON n.id = dr.neighborhood_id
LEFT JOIN run_stops rs ON rs.run_id = dr.id
WHERE dr.state = 'completed'
GROUP BY dr.id, n.name, dr.run_date, dr.total_cost_cents
ORDER BY dr.run_date DESC;
