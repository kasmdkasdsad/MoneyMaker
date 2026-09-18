# Mobile User Flow

## Platform decision: PWA first

Ship a progressive web app, not a native binary. Three reasons, in order of
weight:

1. **The growth loop is a shared link.** This product spreads when a neighbour
   sends "we're 3 short, join?" into a building WhatsApp group. A link that opens
   an app-store page instead of the campaign loses most of that traffic at the
   exact moment intent is highest. A PWA opens *the campaign*, in one tap.
2. **Campaign windows are days long and iteration is weekly.** App review cycles
   are slower than the product's own clock at this stage.
3. Push notifications now work on iOS PWAs (16.4+), which removes the one
   objection that used to be decisive.

Native comes when the captain workflow needs reliable background scanning — a
Phase-2 problem for a few hundred captains, not a launch problem for buyers.

**Constraints this imposes:** first contentful paint under 1.5s on 4G; the whole
app usable one-handed; every primary action inside the thumb arc at the bottom of
the screen.

---

## The core loop

```
   ┌──────────────────────────────────────────────────────┐
   │                                                      │
   ▼                                                      │
DISCOVER ──► JOIN ──► SHARE ──► (threshold met) ──► COLLECT
   ▲          │         │                              │
   │          │         └── the growth engine ─────────┤
   │          │             "3 more and everyone       │
   │          │              pays $11.52"              │
   │          └── no charge yet. this is the            │
   │              trust hinge of the product            │
   └──────────────────────────────────────────────────────┘
                     pickup is a habit anchor:
              same porch, same day, every week
```

Four screens carry the entire product. Everything else is secondary.

---

## Screen 1 — Feed (home)

Ordered by **progress toward threshold, not recency.** A campaign at 90%
converts far better than a fresh one, because joining it feels like *finishing*
something rather than starting something. (`GET /v1/feed` implements exactly
this ordering.)

```
┌────────────────────────────────┐
│  Audubon Park          [◉ 2]   │  ← neighbourhood, not a search bar:
│  Next delivery: Wed            │    this is a local product
├────────────────────────────────┤
│ ┌────────────────────────────┐ │
│ │ [photo]                    │ │
│ │ Cold-pressed olive oil 1L  │ │
│ │ $12.00  ~~$18.99~~         │ │
│ │ ████████████████░░░░  83%  │ │  ← the progress bar IS the product
│ │ 20 of 24 · 4 to go         │ │
│ │ ⏱ closes in 5h            │ │
│ │ 📍 Maple Court · 2 min walk│ │
│ │ ┌────────────────────────┐ │ │
│ │ │       Join · $12       │ │ │  ← thumb zone, always reachable
│ │ └────────────────────────┘ │ │
│ └────────────────────────────┘ │
│ ┌────────────────────────────┐ │
│ │ ✓ SECURED · Detergent 5L   │ │  ← secured campaigns still show:
│ │ $22.00 → $21.00 at 48 units│ │    joining now still helps everyone
│ └────────────────────────────┘ │
├────────────────────────────────┤
│  [ Home ]  [ Orders ]  [ Me ]  │
└────────────────────────────────┘
```

Design notes:

- **The progress bar is the product.** It is the single element that converts a
  price into a reason to act now and a reason to tell someone.
- Show `4 to go`, never `83%`. A countable gap is actionable; a percentage is
  trivia.
- Distance in **walking minutes**, not metres. The question a buyer is actually
  asking is "is this annoying?"
- Secured campaigns stay in the feed with the *next tier* as the call to action.
  The volume after the threshold is where the margin and the virality live.

---

## Screen 2 — Campaign detail → join

```
┌────────────────────────────────┐
│ ←                         ⤴    │
│      [ product photo ]         │
│                                │
│ Cold-pressed olive oil 1L      │
│ Single-estate, this season.    │
│                                │
│ ┌────────────────────────────┐ │
│ │ 20 of 24 · 4 to go         │ │
│ │ ████████████████░░░░       │ │
│ │ ⏱ closes Wed 8pm (5h)     │ │
│ └────────────────────────────┘ │
│                                │
│ GROUP PRICE                    │
│  12+ ........... $12.00  ◄ now │
│  24+ ........... $11.52        │
│  48+ ........... $11.16        │
│ ┌────────────────────────────┐ │
│ │ ℹ If more neighbours join, │ │
│ │   your price drops too.    │ │
│ │   You'll never pay more    │ │
│ │   than $12.00.             │ │
│ └────────────────────────────┘ │
│                                │
│ PICK UP                        │
│ 📍 Maple Court lobby           │
│    Thu–Fri, 8am–8pm            │
│    Hosted by Maya              │
│                                │
│ Quantity   [ − ]  2  [ + ]     │
├────────────────────────────────┤
│ ┌────────────────────────────┐ │
│ │  Join · $24.00 total       │ │
│ └────────────────────────────┘ │
│  Not charged unless it hits 24 │  ← the single most important line
└────────────────────────────────┘
```

**The two lines that carry the whole trust model:**

> *"You'll never pay more than $12.00."*
> *"Not charged unless it hits 24."*

These are not marketing copy — they are the user-facing statements of system
guarantees #2 and #3 in [`03-threshold-algorithm.md`](03-threshold-algorithm.md),
enforced by `plan_lock` and by the `orders_charged_price_not_higher` constraint.
Copy that promises something the system does not guarantee is how a
group-buying product dies; copy that under-sells a guarantee it *does* have
leaves conversion on the table.

The tier ladder is shown in full, with the current tier marked. A buyer should be
able to see, without doing arithmetic, exactly what recruiting more neighbours is
worth to them personally.

---

## Screen 3 — The share moment

Fires immediately after joining, at peak commitment. This screen is the growth
engine; it is not an afterthought.

```
┌────────────────────────────────┐
│           ✓ You're in          │
│                                │
│     3 more and it's locked     │  ← countable gap, not a percentage
│     ████████████████████░      │
│                                │
│  Your card is authorised but   │
│  not charged. If we don't hit  │
│  24 by Wed 8pm, the hold is    │
│  released automatically.       │
│                                │
│ ┌────────────────────────────┐ │
│ │  Share with neighbours     │ │  ← native share sheet
│ └────────────────────────────┘ │
│ ┌────────────────────────────┐ │
│ │  Copy link                 │ │
│ └────────────────────────────┘ │
│                                │
│         Back to browsing       │
└────────────────────────────────┘
```

Share payload is pre-written and does the persuading for them:

> *We're 3 bottles short of unlocking olive oil at $12 (normally $18.99) —
> pickup at Maple Court Thursday. Join: moneymaker.app/c/8f2a*

Why it works: the gap is small and specific, the deadline is real, the pickup
point is a place the recipient knows, and the ask is one tap. The link deep-links
straight to the campaign, which is exactly the thing a native app would break.

---

## Screen 4 — Orders / collect

```
┌────────────────────────────────┐
│  Your orders                   │
├────────────────────────────────┤
│ ┌────────────────────────────┐ │
│ │ 🟢 READY TO COLLECT        │ │
│ │ Olive oil 1L ×2            │ │
│ │ 📍 Maple Court lobby       │ │
│ │ ⏱ by Fri 8pm              │ │
│ │                            │ │
│ │      ┌──────────┐          │ │
│ │      │  4 7 2 9 │          │ │  ← big. read aloud to the captain.
│ │      └──────────┘          │ │    no scanning, no screenshots
│ │                            │ │
│ │ Paid $23.04 (saved $1.92 — │ │
│ │ 6 more neighbours joined)  │ │  ← proof the mechanic is real
│ └────────────────────────────┘ │
│ ┌────────────────────────────┐ │
│ │ ⏳ WAITING · 4 to go       │ │
│ │ Detergent 5L ×1            │ │
│ │ Closes in 5h   [ Share ]   │ │
│ └────────────────────────────┘ │
└────────────────────────────────┘
```

**"Saved $1.92 because 6 more neighbours joined"** is the highest-value sentence
in the app. It converts an abstract mechanic into a felt, personal outcome, and
it is the reason someone shares the *next* campaign without being prompted.

---

## Notifications

Retention lives here. The pickup is weekly, so the app is not a daily habit —
notifications are what carry a buyer from one campaign to the next. Every one
must be an event about *their* money or *their* group.

| Trigger | Copy | Why |
|---|---|---|
| 3 units from threshold, <6h left | "Olive oil is 3 short — closes at 8pm" | Peak share intent |
| Threshold met | "It's happening. Olive oil is locked in for Thursday." | Relief, and the first proof the mechanic works |
| Better tier unlocked | "Price dropped to $11.52 — you'll be charged less." | The single strongest retention message in the product |
| Campaign expired | "Didn't reach 24. **You weren't charged.** Here's what's live now →" | A failed campaign must not become a lost customer |
| Ready for pickup | "Ready at Maple Court. Code 4729. Until Fri 8pm." | Transactional |
| Uncollected, 12h left | "Still at Maple Court until 8pm tomorrow" | Uncollected stock is the captain's biggest complaint |

Hard limit: **one promotional push per campaign per user.** Transactional
messages are exempt. Group buying dies of notification fatigue faster than of
bad pricing, because the deadline mechanic tempts you to send "last chance" four
times.

---

## Edge cases the design must handle

| Situation | Behaviour |
|---|---|
| Flaky connection on join | Client generates the `Idempotency-Key` *before* the first attempt and reuses it on retry. A double-tap returns the same order (`join_campaign` replay check). |
| Campaign fills while the buyer is on the screen | 409 with `insufficient_capacity` → "Only 2 left" with quantity pre-adjusted, not a generic error. |
| Buyer joins at $12, tier drops to $11.52 | Nothing to do. Charged less at lock; the orders screen explains why. |
| Campaign expires | Push, hold released, feed surfaces live alternatives at the same pickup point. |
| Buyer misses the pickup window | Captain marks uncollected; ops decides refund vs. next-run rollover. Modelled as `manifest_items.picked_up_at IS NULL`. |
| Offline | Feed and order codes are cached (service worker). The **pickup code must render offline** — collection happens in a lobby or a garage with no signal. |

---

## Captain view

Same PWA, a different tab. Three jobs and nothing else:

1. **Today's manifest** — grouped by buyer, with pickup codes, sorted by name.
2. **Mark collected** — one tap per buyer; works offline and syncs later.
3. **Earnings** — commission per campaign, next payout date.

Captains are the supply side of the density we sell. The bar is that a captain
can run a 40-parcel handover from a phone in one hand while holding a box in the
other. Anything that needs two hands or a stable connection will not be used.

---

## Accessibility and instrumentation

**Accessibility.** Progress is conveyed by number as well as by bar (never colour
alone); 44px minimum touch targets; the pickup code meets AAA contrast and
respects dynamic type — a chunk of this audience is reading a code in a dim
lobby.

**Instrument exactly these,** in funnel order:

```
feed_view → campaign_view → join_tap → join_success
                                ↓
                         share_sheet_open → share_completed
                                ↓
                    campaign_secured → campaign_locked → collected
```

The two ratios that decide the business: **share rate per join** (does the growth
loop turn?) and **threshold hit rate** (are thresholds set correctly?). If the
first is below ~15% the share moment is wrong; if the second is below ~60% the
thresholds are too high for current density — feed that back into
`min_viable_units` inputs rather than lowering thresholds by hand.
