-- =============================================================================
-- MoneyMaker — Community Group Buying Platform
-- PostgreSQL 15+ schema (MVP, single region)
-- =============================================================================
--
-- DESIGN NOTES
--
-- 1. MONEY is stored as BIGINT minor units (cents). Never FLOAT. Every money
--    column carries a `_cents` suffix so the unit is impossible to misread.
--
-- 2. GEO is stored as plain (lat, lon) DOUBLE PRECISION plus an optional
--    earthdistance GiST index. PostGIS is deliberately NOT a dependency at MVP:
--    at launch scale (<10k pickup points in one metro) a bitmap scan over
--    `cube(ll_to_earth(...))` is ample, and it runs on stock managed Postgres
--    with only contrib extensions. Migrate to `geography(Point,4326)` when
--    polygon service areas or routing-grade queries are needed (Phase 2).
--
-- 3. CONCURRENCY. `campaigns.committed_units` / `reserved_units` are
--    denormalised counters. They are the *authority* for threshold decisions and
--    are only ever mutated while holding a row lock:
--        SELECT ... FROM campaigns WHERE id = $1 FOR UPDATE
--    Contention is scoped per campaign, which is exactly the granularity we
--    want. `db/reconcile.sql` detects drift against the orders table.
--
-- 4. SIDE EFFECTS (payment capture, push notifications, supplier POs) are never
--    performed inside the DB transaction. They are appended to `outbox` in the
--    same transaction as the state change and drained by a worker. This is what
--    makes "threshold met -> capture 200 payments" crash-safe.
--
-- 5. STATE MACHINES are enforced with enums + CHECK constraints + an append-only
--    event log (`campaign_events`). The log is the audit trail we show buyers
--    ("why was I charged?") and captains ("why did this campaign fail?").
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email
CREATE EXTENSION IF NOT EXISTS cube;       -- prerequisite for earthdistance
CREATE EXTENSION IF NOT EXISTS earthdistance;

-- -----------------------------------------------------------------------------
-- Enums
-- -----------------------------------------------------------------------------

CREATE TYPE campaign_state AS ENUM (
    'draft',            -- being configured, not visible to buyers
    'open',             -- accepting commitments
    'locked',           -- threshold met, price fixed, funds captured
    'purchasing',       -- PO issued to supplier
    'in_transit',       -- supplier shipped / on a delivery run
    'ready_for_pickup', -- landed at the pickup point
    'settled',          -- all payouts + refunds reconciled, terminal
    'expired',          -- window closed below threshold, terminal
    'cancelled'         -- killed by ops or supplier, terminal
);

CREATE TYPE order_state AS ENUM (
    'pending_auth',     -- capacity reserved, waiting on PSP authorization
    'committed',        -- authorized; counts toward the threshold
    'locked',           -- campaign locked, funds captured, irrevocable
    'ready_for_pickup',
    'picked_up',        -- terminal happy path
    'expired',          -- campaign missed threshold; authorization voided
    'cancelled',        -- buyer withdrew before lock
    'refunded'          -- post-lock failure, money returned
);

CREATE TYPE payment_state AS ENUM (
    'authorizing', 'authorized', 'captured', 'voided', 'refunded', 'failed'
);

CREATE TYPE temp_zone AS ENUM ('ambient', 'chilled', 'frozen');

CREATE TYPE run_state AS ENUM (
    'planned', 'dispatched', 'completed', 'failed'
);

-- Double-entry accounts. Every cent that moves touches exactly two of these.
CREATE TYPE ledger_account AS ENUM (
    'buyer_escrow',      -- captured funds we are holding
    'supplier_payable',
    'captain_payable',
    'delivery_cost',
    'platform_revenue',
    'refunds_payable'
);

-- -----------------------------------------------------------------------------
-- Shared trigger: maintain updated_at
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- IDENTITY & GEOGRAPHY
-- =============================================================================

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_e164      TEXT        NOT NULL,          -- primary identity: this is a
                                                   -- phone-first, SMS-OTP product
    email           CITEXT,
    display_name    TEXT        NOT NULL,
    -- Approximate home location, used to rank nearby pickup points. Deliberately
    -- coarse (street centroid, not door) to limit blast radius of a breach.
    home_lat        DOUBLE PRECISION,
    home_lon        DOUBLE PRECISION,
    is_captain      BOOLEAN     NOT NULL DEFAULT FALSE,
    is_staff        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT users_phone_e164_format CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    CONSTRAINT users_home_latlon_together CHECK (
        (home_lat IS NULL) = (home_lon IS NULL)
    ),
    CONSTRAINT users_home_lat_range CHECK (home_lat BETWEEN -90 AND 90),
    CONSTRAINT users_home_lon_range CHECK (home_lon BETWEEN -180 AND 180)
);
CREATE UNIQUE INDEX users_phone_key ON users (phone_e164);
CREATE UNIQUE INDEX users_email_key ON users (email) WHERE email IS NOT NULL;
CREATE TRIGGER users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- A neighbourhood is the *delivery batching unit*: one line-haul run serves one
-- neighbourhood on one day. Getting this boundary right is the core last-mile
-- cost lever, so it is a first-class entity rather than a string on an address.
CREATE TABLE neighborhoods (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT        NOT NULL,
    city                TEXT        NOT NULL,
    centroid_lat        DOUBLE PRECISION NOT NULL,
    centroid_lon        DOUBLE PRECISION NOT NULL,
    radius_m            INTEGER     NOT NULL DEFAULT 2500,
    -- The fixed weekly delivery day. Batching every campaign in a neighbourhood
    -- onto one van-day is what makes per-unit last-mile cost collapse.
    delivery_dow        SMALLINT    NOT NULL,      -- 0=Sunday .. 6=Saturday
    -- Cost model inputs, used by pricing.min_viable_units() to derive thresholds.
    line_haul_cost_cents  BIGINT    NOT NULL DEFAULT 0,  -- per run, depot -> zone
    stop_cost_cents       BIGINT    NOT NULL DEFAULT 0,  -- marginal cost per stop
    is_live             BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT neighborhoods_dow_range CHECK (delivery_dow BETWEEN 0 AND 6),
    CONSTRAINT neighborhoods_radius_positive CHECK (radius_m > 0),
    CONSTRAINT neighborhoods_costs_nonneg CHECK (
        line_haul_cost_cents >= 0 AND stop_cost_cents >= 0
    )
);
CREATE TRIGGER neighborhoods_touch BEFORE UPDATE ON neighborhoods
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- The pickup point is the unit of economics. Every order routed here collapses
-- a would-be home delivery into a shared stop.
CREATE TABLE pickup_points (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    neighborhood_id     UUID        NOT NULL REFERENCES neighborhoods(id) ON DELETE RESTRICT,
    host_user_id        UUID        REFERENCES users(id) ON DELETE SET NULL,
    label               TEXT        NOT NULL,       -- "Maple Court lobby"
    address_line        TEXT        NOT NULL,
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    -- How much volume this host can physically hold between drop and pickup.
    -- A garage is not a warehouse; overrunning it is how captains churn.
    capacity_units      INTEGER     NOT NULL DEFAULT 200,
    -- Commission paid to the host, in basis points of campaign gross.
    commission_bps      INTEGER     NOT NULL DEFAULT 500,
    pickup_window_hours INTEGER     NOT NULL DEFAULT 48,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pickup_points_lat_range CHECK (lat BETWEEN -90 AND 90),
    CONSTRAINT pickup_points_lon_range CHECK (lon BETWEEN -180 AND 180),
    CONSTRAINT pickup_points_capacity_positive CHECK (capacity_units > 0),
    CONSTRAINT pickup_points_commission_range CHECK (commission_bps BETWEEN 0 AND 3000)
);
CREATE INDEX pickup_points_neighborhood_idx
    ON pickup_points (neighborhood_id) WHERE is_active;
-- Radius search: "pickup points within N metres of the buyer".
CREATE INDEX pickup_points_earth_idx
    ON pickup_points USING gist (ll_to_earth(lat, lon));
CREATE TRIGGER pickup_points_touch BEFORE UPDATE ON pickup_points
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- CATALOG
-- =============================================================================

CREATE TABLE suppliers (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT        NOT NULL,
    contact_email           CITEXT,
    lead_time_days          INTEGER     NOT NULL DEFAULT 3,
    -- Supplier-side MOQ. A campaign threshold can never be below this.
    min_order_units         INTEGER     NOT NULL DEFAULT 0,
    min_order_value_cents   BIGINT      NOT NULL DEFAULT 0,
    payment_terms_days      INTEGER     NOT NULL DEFAULT 0,
    is_active               BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT suppliers_lead_time_nonneg CHECK (lead_time_days >= 0),
    CONSTRAINT suppliers_moq_nonneg CHECK (
        min_order_units >= 0 AND min_order_value_cents >= 0
    )
);
CREATE TRIGGER suppliers_touch BEFORE UPDATE ON suppliers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE products (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID        NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    title       TEXT        NOT NULL,
    description TEXT,
    category    TEXT        NOT NULL,
    image_urls  TEXT[]      NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX products_supplier_idx ON products (supplier_id);
CREATE INDEX products_category_idx ON products (category);
CREATE TRIGGER products_touch BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE product_variants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id      UUID        NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    sku             TEXT        NOT NULL,
    unit_label      TEXT        NOT NULL DEFAULT 'each',   -- "2kg bag"
    -- Suppliers ship CASES, buyers buy UNITS. Every lock quantity must round to
    -- a whole number of cases; the remainder is real inventory risk that
    -- somebody (platform or captain) has to eat. Modelled explicitly.
    case_pack_units INTEGER     NOT NULL DEFAULT 1,
    weight_grams    INTEGER,
    volume_ml       INTEGER,
    zone            temp_zone   NOT NULL DEFAULT 'ambient',
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT product_variants_case_pack_positive CHECK (case_pack_units >= 1)
);
CREATE UNIQUE INDEX product_variants_sku_key ON product_variants (sku);
CREATE INDEX product_variants_product_idx ON product_variants (product_id);
CREATE TRIGGER product_variants_touch BEFORE UPDATE ON product_variants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- A time-boxed quote from a supplier. Campaigns snapshot the cost from here so
-- a supplier re-quoting mid-campaign cannot retroactively destroy our margin.
CREATE TABLE supplier_offers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id         UUID        NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    variant_id          UUID        NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
    unit_cost_cents     BIGINT      NOT NULL,
    max_units           INTEGER,                    -- NULL = unconstrained
    valid_from          TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to            TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT supplier_offers_cost_positive CHECK (unit_cost_cents > 0),
    CONSTRAINT supplier_offers_window_valid CHECK (valid_to > valid_from),
    CONSTRAINT supplier_offers_max_units_positive CHECK (max_units IS NULL OR max_units > 0)
);
CREATE INDEX supplier_offers_variant_idx
    ON supplier_offers (variant_id, valid_to DESC);

-- =============================================================================
-- CAMPAIGNS  (the group buy itself)
-- =============================================================================

CREATE TABLE campaigns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    variant_id          UUID        NOT NULL REFERENCES product_variants(id) ON DELETE RESTRICT,
    supplier_offer_id   UUID        NOT NULL REFERENCES supplier_offers(id) ON DELETE RESTRICT,
    neighborhood_id     UUID        NOT NULL REFERENCES neighborhoods(id) ON DELETE RESTRICT,
    -- Anchor stop. Additional stops may be attached via campaign_stops when
    -- nearby under-subscribed campaigns are merged (see domain/matching.py).
    pickup_point_id     UUID        NOT NULL REFERENCES pickup_points(id) ON DELETE RESTRICT,
    created_by          UUID        REFERENCES users(id) ON DELETE SET NULL,

    state               campaign_state NOT NULL DEFAULT 'draft',

    -- --- Window -------------------------------------------------------------
    opens_at            TIMESTAMPTZ NOT NULL,
    closes_at           TIMESTAMPTZ NOT NULL,
    -- One-shot "rescue" extension when the campaign closes near-miss.
    extended_until      TIMESTAMPTZ,
    -- First moment committed_units crossed threshold_units. The campaign STAYS
    -- OPEN after this: buyers are told "this is happening", and the extra volume
    -- keeps unlocking better price tiers for everyone already in. We only lock
    -- (capture money, cut the PO) at window close, or early if the cap is hit.
    secured_at          TIMESTAMPTZ,
    locked_at           TIMESTAMPTZ,
    delivery_eta        TIMESTAMPTZ,

    -- --- Thresholds ---------------------------------------------------------
    threshold_units     INTEGER     NOT NULL,   -- fire the order at/above this
    cap_units           INTEGER     NOT NULL,   -- supplier/host ceiling
    case_pack_units     INTEGER     NOT NULL DEFAULT 1,  -- snapshot of variant

    -- --- Live counters (mutated only under SELECT ... FOR UPDATE) -----------
    committed_units     INTEGER     NOT NULL DEFAULT 0,  -- authorized demand
    reserved_units      INTEGER     NOT NULL DEFAULT 0,  -- in-flight authorizations

    -- --- Pricing ------------------------------------------------------------
    unit_cost_cents     BIGINT      NOT NULL,   -- snapshot from supplier_offer
    list_price_cents    BIGINT      NOT NULL,   -- price at threshold (worst tier)
    locked_unit_price_cents BIGINT,             -- set at lock
    final_unit_price_cents  BIGINT,             -- set at settle (may be lower)
    commission_bps      INTEGER     NOT NULL DEFAULT 500,  -- snapshot from host

    -- Optimistic-lock counter for read-modify-write flows outside the row lock.
    version             INTEGER     NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- --- Invariants ---------------------------------------------------------
    CONSTRAINT campaigns_window_valid       CHECK (closes_at > opens_at),
    CONSTRAINT campaigns_extension_forward  CHECK (extended_until IS NULL OR extended_until > closes_at),
    CONSTRAINT campaigns_threshold_positive CHECK (threshold_units > 0),
    CONSTRAINT campaigns_cap_gte_threshold  CHECK (cap_units >= threshold_units),
    CONSTRAINT campaigns_case_pack_positive CHECK (case_pack_units >= 1),
    CONSTRAINT campaigns_counters_nonneg    CHECK (committed_units >= 0 AND reserved_units >= 0),
    -- The oversell guard. Enforced by the database, not just by application code.
    CONSTRAINT campaigns_no_oversell        CHECK (committed_units + reserved_units <= cap_units),
    CONSTRAINT campaigns_prices_positive    CHECK (unit_cost_cents > 0 AND list_price_cents > 0),
    -- A locked campaign must know what it locked at, and must have met threshold.
    CONSTRAINT campaigns_locked_consistent  CHECK (
        state NOT IN ('locked','purchasing','in_transit','ready_for_pickup','settled')
        OR (locked_at IS NOT NULL
            AND locked_unit_price_cents IS NOT NULL
            AND committed_units >= threshold_units)
    ),
    CONSTRAINT campaigns_settled_consistent CHECK (
        state <> 'settled' OR final_unit_price_cents IS NOT NULL
    )
);
CREATE INDEX campaigns_discovery_idx
    ON campaigns (neighborhood_id, state, closes_at)
    WHERE state = 'open';
-- Drives the window-close sweeper: "which open campaigns are past their deadline".
CREATE INDEX campaigns_close_sweep_idx
    ON campaigns (COALESCE(extended_until, closes_at))
    WHERE state = 'open';
CREATE INDEX campaigns_pickup_point_idx ON campaigns (pickup_point_id, state);
CREATE INDEX campaigns_variant_idx ON campaigns (variant_id);
CREATE TRIGGER campaigns_touch BEFORE UPDATE ON campaigns
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Quantity price breaks. The growth engine: more neighbours joining makes the
-- item cheaper for *everyone already in*, which is what makes people share it.
CREATE TABLE price_tiers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    min_units       INTEGER     NOT NULL,
    unit_price_cents BIGINT     NOT NULL,

    CONSTRAINT price_tiers_min_units_positive CHECK (min_units > 0),
    CONSTRAINT price_tiers_price_positive     CHECK (unit_price_cents > 0)
);
CREATE UNIQUE INDEX price_tiers_campaign_min_units_key
    ON price_tiers (campaign_id, min_units);
CREATE INDEX price_tiers_lookup_idx ON price_tiers (campaign_id, min_units DESC);

-- Extra stops merged into a campaign to reach threshold (see matching.py).
CREATE TABLE campaign_stops (
    campaign_id     UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    pickup_point_id UUID        NOT NULL REFERENCES pickup_points(id) ON DELETE RESTRICT,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, pickup_point_id)
);

-- Append-only audit log of every state transition. Never UPDATEd, never DELETEd.
CREATE TABLE campaign_events (
    id              BIGSERIAL PRIMARY KEY,
    campaign_id     UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    event_type      TEXT        NOT NULL,   -- 'opened','committed','locked','expired',...
    from_state      campaign_state,
    to_state        campaign_state,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    actor_user_id   UUID        REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX campaign_events_campaign_idx ON campaign_events (campaign_id, id);

-- =============================================================================
-- ORDERS & PAYMENTS
-- =============================================================================

CREATE TABLE orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id         UUID        NOT NULL REFERENCES campaigns(id) ON DELETE RESTRICT,
    buyer_id            UUID        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    -- Denormalised from campaign at commit time: a buyer may be routed to a
    -- merged stop that is not the campaign's anchor pickup point.
    pickup_point_id     UUID        NOT NULL REFERENCES pickup_points(id) ON DELETE RESTRICT,

    state               order_state NOT NULL DEFAULT 'pending_auth',
    quantity            INTEGER     NOT NULL,

    -- Price the buyer saw when committing. We guarantee they never pay MORE
    -- than this; if tiers improve they pay less and we refund the delta.
    quoted_unit_price_cents BIGINT  NOT NULL,
    charged_unit_price_cents BIGINT,            -- set at capture/settle

    -- Capacity reservation expiry. A pending_auth order that is not confirmed
    -- by this time is swept and its reserved units returned to the pool.
    reserved_until      TIMESTAMPTZ,

    -- Client-supplied idempotency key: double-taps on a flaky mobile connection
    -- must not create two orders.
    idempotency_key     TEXT        NOT NULL,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT orders_quantity_positive CHECK (quantity > 0),
    CONSTRAINT orders_quantity_sane     CHECK (quantity <= 1000),
    CONSTRAINT orders_quoted_price_positive CHECK (quoted_unit_price_cents > 0),
    CONSTRAINT orders_charged_price_not_higher CHECK (
        charged_unit_price_cents IS NULL
        OR charged_unit_price_cents <= quoted_unit_price_cents
    ),
    CONSTRAINT orders_pending_has_reservation CHECK (
        state <> 'pending_auth' OR reserved_until IS NOT NULL
    )
);
CREATE UNIQUE INDEX orders_idempotency_key_key ON orders (idempotency_key);
-- One live order per buyer per campaign. Changing your mind edits the quantity
-- rather than stacking rows, which keeps the counters and the UI honest.
CREATE UNIQUE INDEX orders_one_live_per_buyer_idx
    ON orders (campaign_id, buyer_id)
    WHERE state NOT IN ('cancelled', 'expired', 'refunded');
CREATE INDEX orders_campaign_idx ON orders (campaign_id, state);
CREATE INDEX orders_buyer_idx ON orders (buyer_id, created_at DESC);
CREATE INDEX orders_pickup_point_idx ON orders (pickup_point_id, state);
-- Drives the reservation-expiry sweeper.
CREATE INDEX orders_reservation_sweep_idx
    ON orders (reserved_until) WHERE state = 'pending_auth';
CREATE TRIGGER orders_touch BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Authorization-then-capture. We authorize at commit and capture only at lock,
-- so a failed campaign never touches the buyer's money.
--
-- HARD CONSTRAINT THIS IMPOSES ON THE PRODUCT: card authorizations decay after
-- ~7 days. Campaign windows are therefore capped at 5 days (see app/config.py
-- MAX_CAMPAIGN_WINDOW_DAYS) to leave a safety margin for capture + retries.
CREATE TABLE payment_intents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            UUID        NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    psp                 TEXT        NOT NULL DEFAULT 'stripe',
    psp_intent_ref      TEXT,
    state               payment_state NOT NULL DEFAULT 'authorizing',
    authorized_amount_cents BIGINT  NOT NULL,
    captured_amount_cents   BIGINT  NOT NULL DEFAULT 0,
    refunded_amount_cents   BIGINT  NOT NULL DEFAULT 0,
    authorization_expires_at TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT payment_intents_amounts_nonneg CHECK (
        authorized_amount_cents >= 0
        AND captured_amount_cents >= 0
        AND refunded_amount_cents >= 0
    ),
    CONSTRAINT payment_intents_no_overcapture CHECK (
        captured_amount_cents <= authorized_amount_cents
    ),
    CONSTRAINT payment_intents_no_overrefund CHECK (
        refunded_amount_cents <= captured_amount_cents
    )
);
CREATE UNIQUE INDEX payment_intents_order_key ON payment_intents (order_id);
CREATE UNIQUE INDEX payment_intents_psp_ref_key
    ON payment_intents (psp, psp_intent_ref) WHERE psp_intent_ref IS NOT NULL;
-- Finds authorizations about to decay so we can capture or warn before they do.
CREATE INDEX payment_intents_expiry_idx
    ON payment_intents (authorization_expires_at) WHERE state = 'authorized';
CREATE TRIGGER payment_intents_touch BEFORE UPDATE ON payment_intents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- FULFILMENT & LAST MILE
-- =============================================================================

CREATE TABLE purchase_orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id         UUID        NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    campaign_id         UUID        NOT NULL REFERENCES campaigns(id) ON DELETE RESTRICT,
    -- Rounded UP to a whole number of cases; overage_units is the risk we carry.
    units               INTEGER     NOT NULL,
    overage_units       INTEGER     NOT NULL DEFAULT 0,
    total_cost_cents    BIGINT      NOT NULL,
    issued_at           TIMESTAMPTZ,
    expected_at         TIMESTAMPTZ,
    received_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT purchase_orders_units_positive CHECK (units > 0),
    CONSTRAINT purchase_orders_overage_nonneg CHECK (overage_units >= 0),
    CONSTRAINT purchase_orders_cost_positive  CHECK (total_cost_cents > 0)
);
CREATE UNIQUE INDEX purchase_orders_campaign_key ON purchase_orders (campaign_id);
CREATE TRIGGER purchase_orders_touch BEFORE UPDATE ON purchase_orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- One van, one neighbourhood, one day, many stops. The cost object we optimise.
CREATE TABLE delivery_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    neighborhood_id     UUID        NOT NULL REFERENCES neighborhoods(id) ON DELETE RESTRICT,
    run_date            DATE        NOT NULL,
    carrier             TEXT,
    vehicle_capacity_units INTEGER  NOT NULL DEFAULT 600,
    state               run_state   NOT NULL DEFAULT 'planned',
    -- Actuals, backfilled from the carrier invoice. Compared against the
    -- neighbourhood's modelled costs to keep thresholds honest over time.
    line_haul_cost_cents BIGINT     NOT NULL DEFAULT 0,
    total_cost_cents     BIGINT     NOT NULL DEFAULT 0,
    dispatched_at       TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT delivery_runs_capacity_positive CHECK (vehicle_capacity_units > 0),
    CONSTRAINT delivery_runs_costs_nonneg CHECK (
        line_haul_cost_cents >= 0 AND total_cost_cents >= 0
    )
);
CREATE UNIQUE INDEX delivery_runs_zone_date_key
    ON delivery_runs (neighborhood_id, run_date);
CREATE TRIGGER delivery_runs_touch BEFORE UPDATE ON delivery_runs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE run_stops (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID        NOT NULL REFERENCES delivery_runs(id) ON DELETE CASCADE,
    pickup_point_id     UUID        NOT NULL REFERENCES pickup_points(id) ON DELETE RESTRICT,
    sequence            INTEGER     NOT NULL,
    planned_units       INTEGER     NOT NULL DEFAULT 0,
    delivered_units     INTEGER,
    arrived_at          TIMESTAMPTZ,

    CONSTRAINT run_stops_sequence_nonneg CHECK (sequence >= 0),
    CONSTRAINT run_stops_units_nonneg CHECK (
        planned_units >= 0 AND (delivered_units IS NULL OR delivered_units >= 0)
    )
);
CREATE UNIQUE INDEX run_stops_run_point_key ON run_stops (run_id, pickup_point_id);
CREATE UNIQUE INDEX run_stops_run_sequence_key ON run_stops (run_id, sequence);

-- What the driver hands over, and what the captain scans out to each buyer.
CREATE TABLE manifest_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_stop_id     UUID        NOT NULL REFERENCES run_stops(id) ON DELETE CASCADE,
    order_id        UUID        NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    quantity        INTEGER     NOT NULL,
    picked_up_at    TIMESTAMPTZ,
    pickup_code     TEXT        NOT NULL,   -- 4-digit code the buyer reads out

    CONSTRAINT manifest_items_quantity_positive CHECK (quantity > 0)
);
CREATE UNIQUE INDEX manifest_items_order_key ON manifest_items (order_id);
CREATE INDEX manifest_items_stop_idx ON manifest_items (run_stop_id);

-- =============================================================================
-- MONEY MOVEMENT (double entry)
-- =============================================================================

-- Every financial event writes >= 2 rows summing to zero. `txn_id` groups them.
-- This is what lets us answer "where is this buyer's money right now" without
-- reverse-engineering it from PSP webhooks.
CREATE TABLE ledger_entries (
    id              BIGSERIAL PRIMARY KEY,
    txn_id          UUID        NOT NULL,
    account         ledger_account NOT NULL,
    -- Positive = debit into this account, negative = credit out of it.
    amount_cents    BIGINT      NOT NULL,
    campaign_id     UUID        REFERENCES campaigns(id) ON DELETE SET NULL,
    order_id        UUID        REFERENCES orders(id) ON DELETE SET NULL,
    memo            TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ledger_entries_amount_nonzero CHECK (amount_cents <> 0)
);
CREATE INDEX ledger_entries_txn_idx ON ledger_entries (txn_id);
CREATE INDEX ledger_entries_campaign_idx ON ledger_entries (campaign_id);
CREATE INDEX ledger_entries_account_idx ON ledger_entries (account, created_at DESC);

CREATE TABLE payouts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payee_user_id   UUID        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    campaign_id     UUID        REFERENCES campaigns(id) ON DELETE SET NULL,
    amount_cents    BIGINT      NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'pending',
    psp_transfer_ref TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at         TIMESTAMPTZ,

    CONSTRAINT payouts_amount_positive CHECK (amount_cents > 0)
);
CREATE INDEX payouts_payee_idx ON payouts (payee_user_id, created_at DESC);

-- =============================================================================
-- RELIABILITY PLUMBING
-- =============================================================================

-- Transactional outbox. Written in the SAME transaction as the state change it
-- describes, drained by a worker afterwards. Without this, a crash between
-- "mark campaign locked" and "capture 200 payments" leaves an unrecoverable mess.
CREATE TABLE outbox (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  TEXT        NOT NULL,   -- 'campaign' | 'order'
    aggregate_id    UUID        NOT NULL,
    event_type      TEXT        NOT NULL,   -- 'campaign.locked', 'order.expired'
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- backoff scheduling
    processed_at    TIMESTAMPTZ,
    attempts        INTEGER     NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The worker's hot query: unprocessed and due, oldest first.
CREATE INDEX outbox_pending_idx
    ON outbox (available_at, id) WHERE processed_at IS NULL;
CREATE INDEX outbox_aggregate_idx ON outbox (aggregate_type, aggregate_id);
