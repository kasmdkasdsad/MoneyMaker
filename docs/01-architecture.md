# System Architecture

## The thesis in one paragraph

Group buying is not an e-commerce problem with a social feature bolted on. It is
a **logistics arbitrage**: a van stopping once at a building and handing over 40
parcels costs a fraction of 40 vans stopping 40 times. Everything in this system
— the threshold, the countdown, the price tiers, the captain — exists to
manufacture that density. The software's job is to hold demand in escrow until
enough of it accumulates in one place to be cheap to serve, then commit it
atomically. If the architecture makes that one operation correct, fast and
trustworthy, the business works.

The number that decides whether this company lives is **units delivered per
stop**. Measured against our launch cost model, holding the route constant:

| units/stop | stops | units on run | run cost | **cost per unit** |
|-----------:|------:|-------------:|---------:|------------------:|
| 3          | 8     | 24           | $145.72  | **$6.07**         |
| 6          | 8     | 48           | $145.72  | **$3.04**         |
| 12         | 8     | 96           | $145.72  | **$1.52**         |
| 25         | 8     | 200          | $145.72  | **$0.73**         |
| 40         | 8     | 320          | $145.72  | **$0.46**         |

Same van, same eight stops, same distance. Density alone moves last-mile cost by
13x. Every product and go-to-market decision downstream is a consequence of this
table.

---

## Context

```
   ┌──────────┐      ┌──────────┐      ┌────────────┐
   │  Buyer   │      │ Captain  │      │  Ops /     │
   │ (mobile) │      │ (mobile) │      │ Merchandis.│
   └────┬─────┘      └────┬─────┘      └─────┬──────┘
        │                 │                  │
        └────────┬────────┴─────────┬────────┘
                 │  HTTPS / JSON    │
          ┌──────▼──────────────────▼───────┐
          │        FastAPI application       │
          │  ┌────────────────────────────┐  │
          │  │  functional core (pure)    │  │
          │  │  pricing · thresholds ·    │  │
          │  │  matching · state machine  │  │
          │  └────────────────────────────┘  │
          │  ┌────────────────────────────┐  │
          │  │ imperative shell           │  │
          │  │ campaign_service (txn)     │  │
          │  └────────────────────────────┘  │
          └──────┬───────────────────┬───────┘
                 │                   │ writes
        ┌────────▼────────┐    ┌─────▼──────┐
        │   PostgreSQL    │◄───┤   outbox   │
        │  (system of     │    └─────┬──────┘
        │    record)      │          │ drains
        └─────────────────┘    ┌─────▼──────────────┐
                               │  workers           │
                               │  · outbox drain    │
                               │  · close sweeper   │
                               │  · reservation gc  │
                               └─────┬──────────────┘
                                     │
              ┌──────────────┬───────┴───────┬──────────────┐
              ▼              ▼               ▼              ▼
          Payments       Push/SMS       Supplier EDI     Carrier
          (Stripe)                       (email MVP)     (3PL API)
```

One deployable API, one database, three worker loops. That is the entire MVP.
The complexity budget is spent on correctness in one place — the threshold lock
— rather than on distributing a system that has no scale problem yet.

---

## Components

| Component | Responsibility | Source |
|---|---|---|
| **Functional core** | All decisions. Pure functions over immutable snapshots; no I/O. | `backend/app/domain/` |
| `pricing.py` | Tier ladders, landed cost, **threshold derivation** | |
| `thresholds.py` | Reservation / close / lock / expiry **plans** | |
| `matching.py` | Buyer→stop ranking, campaign merging, run packing | |
| `state.py` | Campaign & order state machines | |
| **Imperative shell** | Takes row locks, applies plans, writes the outbox. Commits nothing itself. | `backend/app/services/campaign_service.py` |
| **API** | Thin HTTP translation. Contains no business rules. | `backend/app/api/` |
| **Workers** | Window-close sweeper, reservation GC, outbox drain | `backend/app/jobs/` |
| **Schema** | The real integrity boundary: CHECKs, partial indexes, enums | `db/schema.sql` |

### Why functional core / imperative shell

The two things that can destroy this business are *overselling a supplier's cap*
and *charging someone incorrectly*. Both are decisions, and both are far easier
to prove correct when the decision is a pure function you can enumerate the
inputs of. `plan_lock()` takes a campaign snapshot and a list of orders and
returns every money movement as data — no database, no Stripe, no clock. That
plan is then executed by a shell that does nothing clever.

The practical payoff: the plan is **replayable**. A worker that dies after
capturing 100 of 300 payments re-derives the identical plan from the persisted
state and finishes, because the plan is a deterministic function of facts that
are still in the database.

---

## The critical path

### Joining a campaign

```
POST /v1/campaigns/{id}/join     Idempotency-Key: <client uuid>
        │
        ├─ auth + idempotency deps resolve first (no DB connection if they fail)
        │
        ├─ BEGIN
        │   ├─ SELECT ... FROM orders WHERE idempotency_key = $1   ← replay check
        │   ├─ SELECT ... FROM campaigns WHERE id = $1 FOR UPDATE  ← the lock
        │   ├─ plan_reservation(snapshot, qty, now)                ← pure
        │   ├─ INSERT order (pending_auth)  +  campaigns.reserved_units += n
        │   └─ INSERT outbox('order.authorize_requested')
        ├─ COMMIT                                    ← lock released here
        │
        └─ [worker] authorize with PSP → confirm_authorization()
                     → pending_auth becomes committed
                     → committed_units += n, secured_at stamped on first crossing
```

The order is `pending_auth` — capacity held, **not** counted toward the
threshold — until the card authorization lands. Without that intermediate state,
a burst of joins on a nearly-full campaign oversells the supplier's cap while
everyone waits on the payment provider.

### Locking

```
[sweeper, every 60s]  campaigns where COALESCE(extended_until, closes_at) <= now
        │                 OR committed_units >= cap_units
        ├─ SELECT ... FOR UPDATE
        ├─ plan_close()  →  LOCK | EXTEND | EXPIRE | NOOP
        │
        ├─ LOCK:   plan_lock() → final tier price, case-rounded purchase qty,
        │          per-order capture instructions → outbox
        ├─ EXTEND: one rescue window (near miss ≥ 80%), never a second
        └─ EXPIRE: void every hold; nobody is charged
```

---

## Decisions and their rationale

| # | Decision | Why | What it costs |
|---|---|---|---|
| 1 | **`SELECT ... FOR UPDATE` on the campaign row**, not `SERIALIZABLE` | Threshold logic is read-decide-write on one counter — a textbook write-skew hotspot. Under `SERIALIZABLE` every concurrent join to a *popular* campaign aborts and retries, so the retry storm peaks exactly when a campaign goes viral. The row lock serialises the same critical section with no retry loop. | Writers to one campaign serialise. Acceptable: campaigns are independent, and per-campaign write rate is low. |
| 2 | **Threshold met ≠ locked.** Crossing the threshold marks the campaign *secured*; it stays open until the window closes. | Locking on contact would trade away the entire growth loop. Post-threshold volume is what walks everyone down the price tiers, and "3 more and everyone pays $11.52" is the highest-converting message in the product. | Fulfilment lead time starts later. Mitigated by locking early when the cap is hit, where there is no upside left. |
| 3 | **Authorize at join, capture at lock** | A failed campaign must never touch anyone's money. "You were never charged" is the difference between a failed campaign and a lost customer. | Card authorizations decay in ~7 days, which **hard-caps campaign windows at 5 days** (`MAX_CAMPAIGN_WINDOW_DAYS`). This is a product constraint imposed by payments, and it is enforced in config, not folklore. |
| 4 | **Transactional outbox** for every external effect | Locking a campaign means capturing hundreds of payments. Doing that inline would hold the campaign row lock across hundreds of network calls, and a crash halfway would leave an unrecoverable mess. | At-least-once delivery, so every handler must be idempotent. PSP idempotency keys carry that weight. |
| 5 | **Money as `BIGINT` cents**, never float or `NUMERIC` | Exact integer arithmetic; rounding direction becomes an explicit, testable decision (costs round up, revenue rounds down). | Callers must format for display. |
| 6 | **Thresholds derived, not chosen** (`min_viable_units`) | The most common way these businesses bleed out is a threshold picked by intuition. Stop cost is fixed per stop, so 6 units can be deeply unprofitable at a price that is healthy at 30. | Requires honest cost inputs, which is why run costs are reconciled against carrier invoices. |
| 7 | **Plain lat/lon + `earthdistance`**, not PostGIS | At launch scale (<10k stops in one metro) a GiST index over `ll_to_earth` is ample and runs on stock managed Postgres with contrib only. Verified using an Index Scan, not a seq scan. | Migrate to `geography(Point,4326)` when polygon service areas or routing-grade queries arrive (Phase 2). |
| 8 | **Phone-first identity** | There is no password to steal, and the phone number *is* the pickup identity the captain verifies against. | SMS costs; needs rate limiting. |
| 9 | **One live order per buyer per campaign** (partial unique index) | Keeps the counters and the UI honest; changing your mind edits a quantity rather than stacking rows. | Multi-basket needs a real cart model later. |

---

## Non-functional targets (MVP)

**Scale.** One metro: ~50 neighbourhoods, ~2k pickup points, ~500 concurrent
campaigns, ~50k orders/week. This is comfortably a single Postgres instance —
tens of gigabytes, low thousands of writes per minute. Anyone proposing sharding
at this stage is solving a problem we would be lucky to have.

**Latency.** Feed < 200ms p95; join < 400ms p95 excluding the PSP round trip
(which is why authorization is asynchronous and the UI never blocks on it).

**The real hotspot** is not throughput, it is the *coordinated* deadline: every
campaign in a neighbourhood closes on the same evening, and buyers pile in during
the final hour. Mitigations: per-campaign locks keep contention local; the
sweeper batches with `LIMIT`; the outbox drains with `FOR UPDATE SKIP LOCKED` so
workers scale horizontally without coordination; a 5s `statement_timeout` stops
a slow query holding a campaign lock hostage.

### Failure modes and responses

| Failure | Response |
|---|---|
| PSP down at join | Order stays `pending_auth`; capacity held; outbox retries with backoff. Reservation GC returns capacity if it never lands. |
| PSP down at lock | Campaign is locked and captures are queued. Outbox retries. Exhausted messages stop retrying and **page a human** — they are money failures, not noise. |
| Authorization expires before capture | `reconcile.sql` query 4 surfaces holds expiring within 48h. The 5-day window cap is what keeps this empty. |
| Worker crashes mid-lock | Plan is re-derived from persisted state and re-applied; handlers are idempotent. |
| Counter drift | `reconcile.sql` queries 1–2 detect it; `plan_lock` prices off the **orders**, not the counter, and emits a warning. |
| Supplier cannot fulfil after lock | Campaign → `cancelled`, orders → `refunded` (captured money returns). The state machine deliberately forbids `cancelled` once stock is `in_transit`; past that it is an operational unwind, not a state flip. |
| Captain drops out mid-campaign | Merge the stop into a neighbouring pickup point (`plan_merge`) or refund. |

### Observability

Track, in order of importance: **units per stop** and **cost per unit delivered**
(the business), **threshold hit rate** and **time-to-secure** (the product),
**outbox backlog age** and **exhausted count** (the money), **counter drift**
(the integrity). `db/reconcile.sql` is written to be run on a schedule with
alerts on any non-empty result.

### Security and privacy

Home locations are stored coarse (street centroid, not door) to limit breach
blast radius. Pickup addresses are only exposed to buyers with a live order at
that stop. Captains see names and pickup codes, never payment details. The
`X-User-Id` header in `deps.py` is a scaffold marked `MUST NOT ship` — it is
replaced by a JWT issued after SMS OTP before launch, along with webhook
signature verification on `/orders/{id}/confirm`.

---

## Deliberately out of scope for MVP

Multi-item carts · cold chain · returns beyond full refund · real-time driver
tracking · in-app chat · multi-currency · a recommendation engine · a mobile app
binary (ship a PWA first, see `03-mobile-flow.md`).

Each of these is a real product need eventually. None of them changes whether
the threshold lock is correct or whether a stop is profitable, which are the
only two things that need to be true for this to work at all.

## Roadmap

- **Phase 0 (weeks 1–4)** — Concierge. One neighbourhood, campaigns run by hand, this schema recording them. Prove the density, not the software.
- **Phase 1 (weeks 5–12)** — This blueprint, shipped. Buyer PWA, captain view, automated lock and capture.
- **Phase 2 (months 4–6)** — PostGIS service areas, an OR-Tools VRP solver behind the `plan_runs` seam, supplier portal, captain payouts automation.
- **Phase 3 (months 7–12)** — Multi-metro, cold chain, a demand forecast that sets thresholds from history instead of from the static cost model.
