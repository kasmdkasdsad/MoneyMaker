# The Threshold-Locking Algorithm

Implementation: [`backend/app/domain/thresholds.py`](../backend/app/domain/thresholds.py)
(pure) and [`backend/app/services/campaign_service.py`](../backend/app/services/campaign_service.py)
(transactional shell).

## What the algorithm has to guarantee

1. **Never oversell.** Committed + in-flight units never exceed the supplier or
   host cap, under any interleaving of concurrent joins.
2. **Never charge for a campaign that did not happen.** Money moves only after
   the threshold is met.
3. **Never charge more than quoted.** More volume may lower a buyer's price; it
   may never raise it.
4. **Be restartable.** A worker dying mid-capture must be able to finish.
5. **Be explicable.** Every buyer and captain can be told exactly why a campaign
   locked, extended or died.

## Campaign lifecycle

```
                       ┌─────────┐
                       │  draft  │
                       └────┬────┘
                            │ publish
                       ┌────▼────┐
      ┌────────────────┤  open   ├──────────────┐
      │                └────┬────┘              │
      │  committed ≥ threshold │  near miss ≥80% │ clear miss
      │  (stamps secured_at,   │  one extension  │
      │   stays OPEN)          │  only           │
      │                        │                 │
      │   window closes OR cap reached           │
      │                        │                 │
 ┌────▼─────┐             (re-enters open)  ┌────▼────┐
 │  locked  │                               │ expired │
 │ · price  │                               │ holds   │
 │   fixed  │                               │ voided  │
 │ · money  │                               └─────────┘
 │   captured                                (terminal)
 └────┬─────┘
      │
 ┌────▼────────┐   ┌────────────┐   ┌──────────────────┐   ┌─────────┐
 │ purchasing  │──►│ in_transit │──►│ ready_for_pickup │──►│ settled │
 └─────────────┘   └────────────┘   └──────────────────┘   └─────────┘
```

**The design decision worth arguing about:** hitting the threshold does *not*
lock the campaign. It stamps `secured_at` — buyers are told "this is happening" —
and the campaign stays open. Locking early would buy a few hours of fulfilment
lead time and cost the entire growth loop, because post-threshold volume is what
walks everyone down the price tiers. The one exception is reaching the cap, where
there is no further upside and we lock immediately.

## Concurrency model

Every mutating flow is the same four steps:

```python
campaign = SELECT * FROM campaigns WHERE id = $1 FOR UPDATE   # 1. lock
snapshot = to_snapshot(campaign)                              # 2. snapshot
plan     = plan_something(snapshot, ...)                      # 3. decide (pure)
apply(plan); write_outbox(plan)                               # 4. apply
# caller commits -> lock released
```

**Lock ordering is campaign first, then its orders in ascending id order.** Any
path that violates this can deadlock against the close sweeper, which locks a
campaign and then everything under it.

**Why a row lock and not `SERIALIZABLE`:** threshold logic reads a counter,
decides, and writes it back — textbook write skew. Under `SERIALIZABLE`, every
concurrent join to a popular campaign aborts and retries, so the retry storm
peaks precisely when a campaign is going viral. The row lock serialises the same
critical section explicitly, with contention scoped to one campaign and no retry
loop. Campaigns are independent, so this does not serialise the site.

## The four decisions

### `plan_reservation` — can this buyer join?

Rejects on: campaign not open, window not started, window closed, insufficient
capacity, invalid quantity. On success returns the units delta, the quoted price,
the amount to authorize, and a reservation deadline.

The quote is the price at the campaign's *current* volume — the **worst** price
the buyer can end up paying, since tiers only improve. We authorize that worst
case and capture less at lock. That ordering is what lets the product promise
"you will never pay more than this" without ever re-prompting for payment.

Reducing or withdrawing always succeeds: it returns capacity to the pool.

### `plan_close` — the window ran out

Priority order:

1. `committed >= threshold` → **LOCK**
2. near miss (`>= 80%` by default), never extended before → **EXTEND** once
3. otherwise → **EXPIRE**

Near-misses get exactly one rescue window. A deadline that can always slip is not
a deadline, and urgency is the only reason anyone shares the link *today* rather
than next week. A campaign with zero commitments is never extended.

### `plan_lock` — commit the buy

```
sold_units       = Σ quantity over counted orders   (orders, not the counter)
final_price      = tier price at sold_units
purchase_units   = round_up_to_case(sold_units)
overage_units    = purchase_units − sold_units      ← inventory risk, reported
per order:
    charge_price = min(final_price, order.quoted_price)   ← quote is a ceiling
    capture      = quantity × charge_price
```

Two details that matter:

- It prices off the **orders**, not `committed_units`. If the counter has drifted
  the orders win, and the plan carries a warning so the drift gets alerted rather
  than silently priced in.
- `min(final_price, quoted_price)` is belt and braces on guarantee #3, and the
  database enforces it independently via `orders_charged_price_not_higher`.

The plan is deterministic: settlements are sorted by order id, so the same inputs
always produce a byte-identical plan. That is what makes it replayable.

### `plan_expiry` — it did not happen

Every uncaptured hold is voided. Nothing is refunded because nothing was ever
captured. The wording of this notification is a commercial decision, not a
technical one: buyers who understand they were never charged come back.

## Worked example

Olive oil, threshold 12, cap 120, case pack 6, tiers `12→$12.00`, `24→$11.52`,
`48→$11.16`.

| step | event | committed | reserved | state | notes |
|---|---|---:|---:|---|---|
| 1 | Rosa joins ×6 | 0 | 6 | open | quoted $12.00; awaiting auth |
| 2 | auth lands | 6 | 0 | open | |
| 3 | Tomas joins ×6, auth lands | 12 | 0 | open | **`secured_at` stamped** — "this is happening". Still open. |
| 4 | 12 more neighbours join ×1 | 24 | 0 | open | crosses the 24-unit tier → $11.52 **for everyone, retroactively** |
| 5 | window closes | 24 | 0 | **locked** | `purchase_units` = 24 (whole cases, no overage) |
| 6 | captures | | | | Rosa quoted $12.00, charged **$11.52** — the difference is never billed |

Had step 4 stalled at 20 units, `plan_close` would have returned **EXTEND** (20/24
= 83%, above the 80% rescue ratio) with one 24-hour window. Had it stalled at 5,
**EXPIRE** and every hold voided.

## Deriving the threshold

The threshold itself is not a product decision — it is solved for
(`pricing.min_viable_units`):

```
contribution_per_unit = price − supplier_cost − line_haul/unit
                              − commission − payment fees
min_units             = ceil((stop_cost + target_margin) / contribution_per_unit)
                        rounded up to a whole case, lifted to supplier MOQ
```

If `contribution_per_unit <= 0` the function refuses to return a threshold and
reports that the item cannot be made to work at that price — no quantity rescues
an item sold below landed cost. The result names the **binding constraint**
(`economics` / `case_pack` / `supplier_moq`) so a merchandiser knows which lever
to pull. Exposed at `POST /v1/ops/threshold-preview`.

## Failure analysis

| Scenario | Behaviour |
|---|---|
| Two buyers race for the last slot | Both block on the campaign row lock; the second sees `available_units` already consumed and is rejected with "only N units left". Verified by `test_in_flight_reservations_count_against_the_cap`. |
| Buyer abandons checkout | `reserved_until` lapses; the reservation sweeper returns the capacity. Verified by `test_abandoned_checkouts_release_their_capacity`. |
| Buyer withdraws after securing | Capacity returns and `secured_at` is **cleared** if it drops back below threshold — buyers must not be told a campaign is happening when it no longer is. |
| Worker dies after 100 of 300 captures | Processed messages are marked; unprocessed ones are re-claimed. `plan_lock` re-derives identically. Captures are idempotent at the PSP. |
| Campaign locks, supplier cannot ship | `locked → cancelled`, orders `→ refunded`. The state machine forbids `cancelled` once `in_transit`. |
| Counter drift | Orders win; a warning rides on the lock plan; `reconcile.sql` alerts. |

## Test coverage

117 tests, all passing. 100 are pure-domain and need no database; 17 run the full
money path against real PostgreSQL:

```
tests/test_pricing.py       20   tier ladders, threshold derivation, overage
tests/test_thresholds.py    38   reservation / close / lock / expiry, simulation
tests/test_matching.py      27   ranking, merging, run packing, cost curve
tests/test_state.py         10   transition legality
tests/test_api.py            8   HTTP contracts and guards
tests/test_integration_postgres.py
                            17   end-to-end on PostgreSQL 16
```

The integration tests are the ones that matter, because the guarantees depend on
database behaviour — `FOR UPDATE` serialisation, the oversell `CHECK`, partial
unique indexes — that has no SQLite equivalent. Testing them against a substitute
engine would prove nothing, so they skip unless `MM_TEST_DATABASE_URL` is set.
