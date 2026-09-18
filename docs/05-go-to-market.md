# Go-to-Market: Engineering Last-Mile Cost Down

## The strategic premise

Most group buying startups die of the same thing: they treat geography as a
market to be covered and discover, around month eight, that their delivery cost
per unit never fell. It never fell because they spread thin. Spreading thin is
the one thing this business model cannot survive, because **last-mile cost per
unit is a function of density, and density is a function of deliberately
refusing to expand.**

Here is the whole argument in one table — our launch cost model, holding the
route constant at eight stops:

| units/stop | units on run | run cost | **cost per unit** |
|-----------:|-------------:|---------:|------------------:|
| 3          | 24           | $145.72  | **$6.07**         |
| 6          | 48           | $145.72  | **$3.04**         |
| 12         | 96           | $145.72  | **$1.52**         |
| 25         | 200          | $145.72  | **$0.73**         |
| 40         | 320          | $145.72  | **$0.46**         |

Same van, same eight stops, same kilometres. A 13x swing in the only cost that
matters, driven entirely by how much volume sits at each stop. On a $12 bottle of
olive oil, $6.07/unit of delivery cost is a dead business and $0.46 is a great
one.

So the go-to-market plan is not "acquire users." It is **manufacture density,
one building at a time, and refuse to serve anything below break-even stop
volume.** Every tactic below is downstream of that sentence.

---

## The five levers, ranked by impact

### Lever 1 — Pickup points, not home delivery (10–40x)

This is the entire business. A van that stops once at an apartment lobby and
hands 40 parcels to a resident host has replaced 40 separate home deliveries.
Nothing else available to us moves cost by an order of magnitude.

The consequence for product: the pickup point is a first-class entity
(`pickup_points`), and buyer→stop matching actively prefers a stop that already
has a live campaign — `rank_pickup_points` scores such a stop as if it were 400m
closer. We will deliberately ask a buyer to walk an extra three minutes rather
than open a second stop, because the second stop costs $12 and the walk costs
nothing.

**Never offer home delivery, not even as a paid upgrade, in year one.** It
cannibalises the density that makes the prices possible, and the customers it
attracts are the ones who will not tolerate the pickup model anyway.

### Lever 2 — One delivery day per neighbourhood (3–5x)

Every campaign in a neighbourhood lands on the same van on the same day.
Batching across campaigns is what fills the truck: three separate 12-unit
campaigns on one Wednesday run is a 36-unit stop, not three unprofitable ones.

This is why `neighborhoods.delivery_dow` exists and why campaign windows are set
to close against it. It also produces a habit — "Wednesday is pickup day" — which
is worth more for retention than any promotion we could run.

### Lever 3 — Thresholds derived from cost, never chosen (prevents the slow bleed)

The threshold is not a growth dial. It is the answer to "at what quantity does
this stop stop losing money," and it is solved for, not picked
(`pricing.min_viable_units`, exposed at `POST /v1/ops/threshold-preview`).

Break-even minimums under our launch cost model ($12 stop cost, $60 line haul
over ~300 units, 5% captain commission, 2.9% + 30¢ payments):

| item | price | supplier cost | case pack | contribution/unit | **break-even** | **with $15 target margin** | binding constraint |
|---|---:|---:|---:|---:|---:|---:|---|
| Olive oil 1L      | $12.00 | $7.00  | 6  | $3.70 | 6  | **12** | case pack |
| Detergent 5L      | $22.00 | $14.50 | 4  | $5.41 | 4  | **8**  | case pack |
| Rice 10kg         | $18.00 | $11.50 | 4  | $4.72 | 4  | **8**  | case pack |
| Pet food 12kg     | $42.00 | $31.00 | 2  | $7.33 | 2  | **4**  | economics |
| Paper towels ×12  | $16.00 | $10.50 | 6  | $3.88 | 24 | **24** | supplier MOQ |

Two things to read out of this table. First, **case pack is usually the binding
constraint, not the economics** — so the highest-leverage supplier negotiation is
often about smaller cases, not lower prices. Second, we run campaigns at the
target-margin column, not break-even, because break-even is not a business:

```
 12 units → $10.20 contribution   ($1.70/unit)
 24 units → $32.40 contribution   ($2.70/unit)
 48 units → $76.80 contribution   ($3.20/unit)
```

Contribution per unit keeps climbing well past the threshold. That is the
economic reason the campaign stays open after it is secured.

### Lever 4 — Merge stops, but only when the stop pays for itself

An under-subscribed campaign can absorb demand from a nearby stop for the same
item (`plan_merge`). The discipline that makes this a cost lever rather than a
cost leak: a candidate stop must carry at least `min_units_per_stop` units, or it
is rejected **even though absorbing it would clear the threshold**. Adding 2
units to rescue a campaign, at the price of a whole $12 van stop, turns a failed
campaign into a losing one.

### Lever 5 — Buy the route, not the stop

Carrier contracts are a strategic choice, not a procurement detail:

- **Per-stop pricing** makes our cost structure move against us exactly as we
  succeed at densifying.
- **Hourly / per-route ("milk run") pricing** means every extra unit we cram onto
  an existing stop is free at the margin.

Negotiate hourly from day one, even at a worse headline rate. Reconcile modelled
costs against actual carrier invoices (`delivery_runs.total_cost_cents` vs.
`estimate_run_cost_cents`) and feed the truth back into
`neighborhoods.stop_cost_cents` — otherwise every threshold in the system is
derived from a fiction.

---

## Phased launch

### Phase 0 — Concierge (weeks 1–4). Prove density, not software.

**Geography: three adjacent apartment complexes. Not a zip code. Not a city.**

Run campaigns by hand: a WhatsApp group, a spreadsheet, a rented van, the
founders doing the handover. No app.

- Recruit 3 captains (see playbook below) — one per building.
- Run 2 campaigns/week, 4 SKUs, all ambient and non-perishable.
- Buy from a cash-and-carry wholesaler. No supplier contracts yet.

**Gate to Phase 1:** ≥ 12 units per stop, ≥ 60% of campaigns hitting threshold,
≥ 40% of buyers ordering twice. If density is not there with founders doing the
work by hand, software will not create it.

### Phase 1 — One neighbourhood, automated (weeks 5–12)

Ship this blueprint. Expand to ~8 pickup points inside a **single 2.5km radius**
— `neighborhoods.radius_m` is a real constraint, not a display field.

- Fixed Wednesday delivery. One van, one route, 8 stops.
- 8–12 live SKUs, weekly rotation.
- Captains onboarded via a written playbook, paid 5% of stop gross.
- Contract the first two suppliers on terms, targeting smaller case packs.

**Gate to Phase 2:** cost per unit delivered < $1.00; ≥ 25 units/stop; captain
month-2 retention ≥ 80%.

### Phase 2 — Second neighbourhood, same city (months 4–6)

Clone, do not stretch. The second neighbourhood gets its own delivery day, its
own van-day, its own captains. Shared: suppliers, the depot, the software.

The test that matters: does neighbourhood #2 reach 25 units/stop **faster** than
#1 did? If not, the playbook is not yet a playbook and the answer is to fix it,
not to open #3.

### Phase 3 — Metro density (months 7–12)

Five to eight neighbourhoods, two delivery days, two vans. Only now does route
optimisation stop being a heuristic: swap an OR-Tools VRP solver in behind the
`plan_runs` seam. Only now does cold chain become worth its capital and spoilage
risk.

---

## Captain recruitment playbook

Captains are the supply side of density. One captain is worth more than a hundred
scattered buyers, because a captain *is* a stop.

**Where they are:** building WhatsApp/Facebook groups, HOA boards, the person who
already organises the building's Amazon returns or holiday party. Every building
has one. Find that person specifically.

**The pitch, in this order:**

1. *"Your neighbours already want this. You just host the box."*
2. 5% of everything collected at your door, paid weekly.
3. Your own orders are free of the walk.
4. Two hours a week, on your schedule, inside a 48-hour pickup window.

**Realistic numbers to put in front of them:** a 200-unit stop at $12 average is
$2,400 gross → **$120/week** commission. That is real money for two hours of
low-effort work at home, and it is honest — it is exactly what
`pickup_points.commission_bps` pays.

**What makes captains quit** (in order): unexpected volume overrunning their
space, buyers collecting late, and unclear pay. The system answers each directly
— `capacity_units` caps what can land on them, `pickup_window_hours` bounds their
exposure, and the earnings tab shows commission per campaign. Respect these and
retention is high; ignore them and you re-recruit the supply side every month.

---

## Category strategy

**Year one: ambient, non-perishable, high value per cubic metre, predictable
repurchase.** Pantry staples, household consumables, pet food, paper goods,
cleaning supplies.

| Screen | Why it matters |
|---|---|
| Ambient only | Cold chain adds refrigerated vehicles, spoilage risk and a hard pickup deadline. It is a Phase-3 capability, not a launch feature. |
| High value density | The van is volume-constrained before it is weight-constrained. $/m³ decides how much margin one run can carry. |
| Predictable repurchase | Repeat demand is what makes the *second* campaign at a stop easy, and the second campaign is where the economics actually arrive. |
| Small case packs | Usually the binding constraint on the threshold (see the table above). Negotiate case size as hard as unit price. |
| Obvious reference price | The saving must be legible in one glance, or the progress bar has nothing to sell. |

Explicitly avoid at launch: fresh produce, dairy, frozen, fashion/sizing,
anything fragile, anything with a serial number or a warranty claim path.

---

## Metrics and gates

**The one number:** cost per unit delivered. Everything else is diagnostic.

| Metric | Launch target | Why |
|---|---|---|
| Units per stop | ≥ 25 | The lever. Below ~12 most campaigns lose money. |
| Cost per unit delivered | < $1.00 | Compare against the density table. |
| Threshold hit rate | ≥ 60% | Below this, thresholds exceed real density — fix the *inputs* to `min_viable_units`, do not lower thresholds by hand. |
| Contribution margin per run | > $150 | The run is the P&L unit, not the order. |
| Captain retention (month 2) | ≥ 80% | Losing a captain deletes a whole stop. |
| Buyer repeat rate (4 weeks) | ≥ 40% | Habit forming around the weekly pickup. |
| Share rate per join | ≥ 15% | Is the growth loop actually turning? |
| Case overage as % of units | < 3% | Silent margin leak (`purchase_orders.overage_units`). |

Run `db/reconcile.sql` on a schedule. Query 8 reports units per stop and cost per
unit for every completed run — that is the weekly business review in one query.

---

## Pre-launch demand aggregation

Do not open a pickup point and hope. **Aggregate demand before committing a
stop**, using the same threshold mechanic that runs the campaigns:

1. Landing page per building: *"Unlock group prices at Maple Court — 18 of 25
   neighbours signed up."*
2. At 25 signups, recruit a captain from that list — the people who signed up
   first are the natural candidates.
3. Only then run campaign #1, with demand already proven.

This makes the first campaign at a new stop hit its threshold, which matters
disproportionately: a stop whose first campaign fails rarely gets a second, and
the captain usually quits.

---

## Risks

| Risk | Mitigation |
|---|---|
| **Captain churn deletes a stop** | Recruit a backup host per building from day one; `plan_merge` can fold orphaned demand into a neighbouring stop rather than refunding it. |
| **Thresholds set above real density** → cascade of failed campaigns, buyers stop trusting the mechanic | Derive from the cost model; monitor hit rate weekly; the 80% rescue extension catches near-misses. A failed campaign must always end with "you were never charged." |
| **A competitor with capital undercuts on price** | They cannot undercut density, and density is local and slow to build. Depth in one metro beats thin coverage of five. |
| **Supplier MOQ exceeds what a stop can absorb** | Merge stops (`plan_merge`); negotiate case pack down; drop the SKU. The threshold preview makes this visible *before* the campaign opens. |
| **Carrier moves to per-stop pricing as we densify** | Hourly contracts negotiated up front; keep a second carrier warm. |
| **Case overage quietly eats margin** | Modelled explicitly, charged at full cost in `campaign_contribution_cents`, reported on every lock plan. |
| **Payment authorizations decay before capture** | 5-day window cap enforced in config; `reconcile.sql` query 4 alerts on holds expiring within 48h. |

---

## First 90 days

| Weeks | Focus | Exit condition |
|---|---|---|
| 1–2 | Recruit 3 captains across 3 buildings; source 4 SKUs from cash-and-carry | 3 signed captains, 75+ waitlist signups |
| 3–4 | Run 4 concierge campaigns by hand | ≥ 12 units/stop, ≥ 60% hit rate |
| 5–8 | Ship Phase 1: PWA, threshold engine, captain view | First fully automated lock-and-capture |
| 9–10 | Expand to 8 pickup points in one 2.5km radius; contract 2 suppliers | ≥ 20 units/stop |
| 11–12 | Negotiate hourly carrier contract; reconcile modelled vs. actual costs | Cost per unit < $1.00 |
| 13 | Go/no-go on neighbourhood #2 | All Phase-1 gates met |

The discipline that makes this work is the willingness to say no to the second
neighbourhood until the first one is dense. Every failed company in this category
said yes too early.
