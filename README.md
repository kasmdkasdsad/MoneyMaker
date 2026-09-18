# MoneyMaker

A community-driven group buying platform. Neighbours pool demand on a product;
when enough units commit, the order locks at a group price and lands as a single
delivery at a shared pickup point.

This repository is an **MVP blueprint**: a working backend for the parts that are
hard to get right, plus the architecture, product and go-to-market thinking
around them.

---

## The thesis

Group buying is not e-commerce with a social feature. It is logistics arbitrage:
one van stopping once at a building with 40 parcels costs a fraction of 40 home
deliveries. Holding our route constant at eight stops:

| units/stop | run cost | **cost per unit delivered** |
|-----------:|---------:|----------------------------:|
| 3          | $145.72  | **$6.07** |
| 12         | $145.72  | **$1.52** |
| 40         | $145.72  | **$0.46** |

Same van, same stops, same distance — a 13x swing driven purely by density. The
threshold mechanic exists to manufacture that density; the go-to-market plan
exists to protect it. (Figures computed by `app.domain.pricing` and
`app.domain.matching`, not estimated.)

---

## The blueprint

| | Document |
|---|---|
| 1 | [System architecture](docs/01-architecture.md) — components, critical path, decisions and their costs, failure modes |
| 2 | [Data model](docs/02-data-model.md) — PostgreSQL schema and the five decisions that shape it |
| 3 | [Threshold-locking algorithm](docs/03-threshold-algorithm.md) — guarantees, concurrency model, worked example, failure analysis |
| 4 | [Mobile user flow](docs/04-mobile-flow.md) — four screens, the growth loop, notification discipline |
| 5 | [Go-to-market](docs/05-go-to-market.md) — five levers for last-mile cost, phased launch, captain playbook, unit economics |

## The code

```
db/
  schema.sql        20 tables, 52 CHECK constraints, 57 indexes (PostgreSQL 16)
  seed.sql          demo neighbourhood with campaigns in each interesting state
  reconcile.sql     8 operational integrity + efficiency queries
backend/app/
  domain/           functional core — pure, no I/O
    pricing.py        tier ladders, landed cost, THRESHOLD DERIVATION
    thresholds.py     reservation / close / lock / expiry decisions
    matching.py       buyer→stop ranking, campaign merging, run packing
    state.py          campaign + order state machines
  services/         imperative shell — row locks, plan execution, outbox
  jobs/             close sweeper, reservation GC, outbox drain
  api/              FastAPI routes (thin; no business rules)
```

The design is **functional core / imperative shell**. Every decision is a pure
function returning a *plan*; a thin transactional shell applies it. The two
things that can destroy this business — overselling a supplier's cap and
charging someone incorrectly — are decisions, and decisions are far easier to
prove correct when they have no I/O in them. It also makes plans replayable: a
worker that dies after capturing 100 of 300 payments re-derives the identical
plan and finishes.

### Three ideas worth the read

**Thresholds are derived, not chosen.** The most common way these businesses
bleed out is a minimum picked by intuition. Stop cost is fixed per stop, so 6
units can be deeply unprofitable at a price that is healthy at 30.
`pricing.min_viable_units()` solves for the minimum and names the binding
constraint (`economics` / `case_pack` / `supplier_moq`) so a merchandiser knows
which lever to pull. If the item cannot work at any volume, it says so instead of
inventing a number.

**Hitting the threshold does not lock the campaign.** It marks it *secured* —
buyers are told it is happening — and the campaign stays open, because
post-threshold volume is what walks everyone down the price tiers. Locking on
contact would buy a few hours of lead time and cost the entire growth loop. The
one exception is reaching the cap, where there is no upside left in waiting.

**`reserved_units` is not the same as `committed_units`.** A card authorization
takes seconds, and during those seconds a buyer must hold a slot (or a burst
oversells the cap) without moving the threshold (or a campaign locks on money
that never arrived). Conflating the two is a real bug in most implementations.

---

## Running it

```bash
make db-up            # PostgreSQL + schema + seed data
make test             # 100 unit tests, no database needed
make test-integration # + 17 end-to-end tests against real PostgreSQL
make api              # http://localhost:8000/docs
```

### Test status

```
117 passed
  100  pure domain — pricing, thresholds, matching, state machines, HTTP contracts
   17  end-to-end on PostgreSQL 16 — the full money path
```

The integration tests carry the weight, because the guarantees depend on database
behaviour with no SQLite equivalent: `SELECT ... FOR UPDATE` serialisation, the
`campaigns_no_oversell` CHECK, and partial unique indexes. Testing those against a
substitute engine would prove nothing, so they skip unless `MM_TEST_DATABASE_URL`
is set.

Everything in this repository has been executed: the schema applies cleanly under
`ON_ERROR_STOP=1`, every constraint above was verified *firing*, and the
earthdistance radius query was confirmed to use an index scan.

---

## Status and honest limits

This is a blueprint with a working core, not a deployable product. Known gaps,
all marked in the code:

- **Authentication is a scaffold.** `api/deps.py` trusts an `X-User-Id` header
  and is marked `MUST NOT ship`. Replace with a JWT issued after SMS OTP.
- **`/orders/{id}/confirm` needs webhook signature verification** before it is
  publicly exposed.
- **No frontend.** `docs/04-mobile-flow.md` specifies it; no code implements it.
- **Fulfilment is modelled, not built.** `purchase_orders`, `delivery_runs`,
  `run_stops` and `manifest_items` exist in the schema and the ORM, and
  `matching.plan_runs` packs and sequences routes, but no service wires a locked
  campaign through to a dispatched van.
- **Payments are a Protocol with a fake implementation.** The Stripe adapter is
  not written; `FakePaymentGateway` models the behaviour that bites (holds
  expire, expired holds fail to capture).
- **No migrations tool.** `schema.sql` is applied directly; adopt Alembic before
  the first production schema change.
