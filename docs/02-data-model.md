# Data Model

Full DDL: [`db/schema.sql`](../db/schema.sql) — 20 tables, 52 CHECK constraints,
57 indexes, verified against PostgreSQL 16.

## Entity relationships

```
                    ┌──────────────┐
                    │ neighborhoods│  the delivery batching unit:
                    │  · delivery_ │  one van-day serves one zone
                    │    dow       │
                    │  · line_haul │──┐ cost model inputs feed
                    │  · stop_cost │  │ min_viable_units()
                    └──────┬───────┘  │
                           │ 1:N      │
                    ┌──────▼───────┐  │
      users ───────►│ pickup_points│  │   THE UNIT OF ECONOMICS
      (host)        │  · capacity  │  │   every order routed here collapses
                    │  · commission│  │   a home delivery into a shared stop
                    └──────┬───────┘  │
                           │ 1:N      │
  suppliers            ┌───▼──────────▼──┐        ┌─────────────┐
      │                │   campaigns     │───1:N─►│ price_tiers │
      ├─► products     │  · state        │        └─────────────┘
      │      │         │  · threshold    │
      │      └─► product_variants ──────►│  · cap         ┌──────────────────┐
      │             · case_pack_units    │  · committed  ─┤ campaign_events  │
      │                                  │  · reserved    │ (append-only)    │
      └─► supplier_offers ──────────────►│  · secured_at  └──────────────────┘
             · unit_cost (snapshotted)   └───┬─────────┘
                                             │ 1:N
                                       ┌─────▼──────┐        ┌────────────────┐
                              users ──►│   orders   │──1:1──►│ payment_intents│
                             (buyer)   │  · quoted  │        │  · auth expiry │
                                       │  · charged │        └────────────────┘
                                       └─────┬──────┘
                                             │
   ┌──────────────┐   ┌───────────┐    ┌─────▼──────────┐
   │ delivery_runs│──►│ run_stops │───►│ manifest_items │  what the captain
   │ · line_haul  │   │ · seq     │    │ · pickup_code  │  scans out
   └──────────────┘   └───────────┘    └────────────────┘

   purchase_orders · ledger_entries · payouts · outbox
```

## The five decisions that shape this schema

### 1. Money is `BIGINT` cents, always

Every monetary column carries a `_cents` suffix so the unit cannot be misread at
a call site. No floats, no `NUMERIC`. The only operations needed are add,
multiply by a rational, and round in a *stated* direction — and making the
rounding direction explicit is what keeps a fraction of a cent from becoming a
reconciliation bug.

### 2. Counters are denormalised, and the database enforces the invariant

`campaigns.committed_units` and `reserved_units` are the authority for threshold
decisions. Summing `orders` on every join would be correct but would put the
hottest path in the product behind an aggregate. So the counters are cached —
and then guarded:

```sql
CONSTRAINT campaigns_no_oversell
    CHECK (committed_units + reserved_units <= cap_units)
```

This is not decoration. Application bugs happen; a `CHECK` is the last line that
stops us promising a supplier's stock twice. It is verified firing in
`tests/test_integration_postgres.py::TestOversellProtection`, and drift against
the source of truth is detected by `db/reconcile.sql` queries 1–2.

Similarly, a locked campaign cannot exist without the price it locked at:

```sql
CONSTRAINT campaigns_locked_consistent CHECK (
    state NOT IN ('locked','purchasing','in_transit','ready_for_pickup','settled')
    OR (locked_at IS NOT NULL AND locked_unit_price_cents IS NOT NULL
        AND committed_units >= threshold_units)
)
```

### 3. `reserved_units` exists because payment providers are slow

Three distinct quantities, and conflating any two of them causes a real bug:

| | meaning | counts toward threshold? | holds capacity? |
|---|---|---|---|
| `reserved_units` | authorization in flight | **no** | yes |
| `committed_units` | authorized, campaign open | yes | yes |
| purchased units | case-rounded, sent to supplier | — | — |

A `pending_auth` order must hold a slot (or a burst oversells the cap while
everyone waits on Stripe) but must **not** move the threshold (or a campaign
locks on money that never arrived).

### 4. Case packs are modelled, because suppliers ship cases and buyers buy units

`product_variants.case_pack_units` propagates to `campaigns.case_pack_units` and
forces every lock quantity up to a whole case. The remainder is
`purchase_orders.overage_units` — real inventory we own and have not sold. Most
blueprints skip this; it is exactly the kind of quiet cost that makes a campaign
look profitable in a dashboard and not be. `campaign_contribution_cents()`
charges overage at full supplier cost.

Case pack is frequently the *binding constraint* on a threshold — see the table
in [`05-go-to-market.md`](05-go-to-market.md).

### 5. Append-only where the truth must survive

- `campaign_events` — every state transition, never updated or deleted. This is
  what answers "why was I charged?" for a buyer and "why did my campaign fail?"
  for a captain.
- `ledger_entries` — double entry, grouped by `txn_id`, each group summing to
  zero (`reconcile.sql` query 7). Lets us answer "where is this buyer's money
  right now" without reverse-engineering PSP webhooks.
- `outbox` — written in the same transaction as the state change it describes.

## Index strategy

Indexes exist for named access paths, not speculatively:

| Index | Serves |
|---|---|
| `campaigns_discovery_idx (neighborhood_id, state, closes_at) WHERE state='open'` | the buyer feed |
| `campaigns_close_sweep_idx (COALESCE(extended_until, closes_at)) WHERE state='open'` | the window-close sweeper |
| `orders_one_live_per_buyer_idx (campaign_id, buyer_id) WHERE state NOT IN (...)` | one live order per buyer; cancelled orders free the slot |
| `orders_reservation_sweep_idx (reserved_until) WHERE state='pending_auth'` | reservation GC |
| `outbox_pending_idx (available_at, id) WHERE processed_at IS NULL` | the worker's hot query |
| `payment_intents_expiry_idx (authorization_expires_at) WHERE state='authorized'` | finding holds before they decay |
| `pickup_points_earth_idx USING gist (ll_to_earth(lat, lon))` | "stops within N metres" |

Partial indexes throughout: the sweepers only ever care about a small live subset,
so indexing terminal rows would be paying storage and write cost for nothing.

## On PostGIS

Deliberately not a dependency. At launch scale a GiST index over
`ll_to_earth(lat, lon)` answers radius queries with an index scan (verified) and
needs only contrib extensions available on stock managed Postgres. The migration
to `geography(Point,4326)` is a Phase-2 change, triggered by the first real need
for polygon service areas or routing-grade distance — not before.
