"""Pricing, landed-cost and threshold-derivation maths.

Everything here is a pure function over integers. Money is **cents** (int); there
is no float arithmetic anywhere in this module, and no ``Decimal`` either -- the
only operations we need are add, multiply by a rational, and round in a stated
direction.

The centrepiece is :func:`min_viable_units`, which answers the question a group
buying company lives or dies on:

    "How many units must commit at this stop before shipping it stops losing
    money?"

Most group buying products pick thresholds by vibes ("let's say 20"). That is
the single most common way these businesses bleed out: the last-mile stop cost
is fixed per stop, so a stop with 6 units on it can be deeply unprofitable at
the very same unit price that is healthy at 30 units. We derive the threshold
from the cost model instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

BPS = 10_000


# ---------------------------------------------------------------------------
# Rounding helpers
#
# Rounding direction is a business decision, not an implementation detail, so it
# is always explicit at the call site. Costs round UP, revenue rounds DOWN: we
# would rather quote a threshold slightly too high than ship at a loss.
# ---------------------------------------------------------------------------


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer ceiling division. Requires a positive denominator."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return -(-numerator // denominator)


def round_up_to_multiple(value: int, multiple: int) -> int:
    """Round ``value`` up to the nearest multiple of ``multiple``."""
    if multiple <= 0:
        raise ValueError("multiple must be positive")
    return ceil_div(value, multiple) * multiple


def apply_bps(amount_cents: int, bps: int, *, round_up: bool) -> int:
    """Take ``bps`` basis points of ``amount_cents``."""
    if bps < 0:
        raise ValueError("bps must be non-negative")
    if round_up:
        return ceil_div(amount_cents * bps, BPS)
    return (amount_cents * bps) // BPS


# ---------------------------------------------------------------------------
# Price tiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class PriceTier:
    """A quantity price break: at ``min_units`` or more, everyone pays
    ``unit_price_cents``."""

    min_units: int
    unit_price_cents: int

    def __post_init__(self) -> None:
        if self.min_units <= 0:
            raise ValueError("tier min_units must be positive")
        if self.unit_price_cents <= 0:
            raise ValueError("tier unit_price_cents must be positive")


class TierConfigurationError(ValueError):
    """Raised when a tier ladder would misprice a campaign."""


def validate_tiers(tiers: Sequence[PriceTier]) -> list[PriceTier]:
    """Validate and normalise a tier ladder, returning it sorted by quantity.

    Enforces the invariant the whole growth loop rests on: **buying more never
    costs more per unit**. A ladder that violates this would let a buyer's own
    referral push their price up, which is both a trust disaster and, because we
    guarantee ``charged <= quoted``, a direct revenue leak.
    """
    if not tiers:
        raise TierConfigurationError("a campaign needs at least one price tier")

    ordered = sorted(tiers, key=lambda t: t.min_units)

    seen: set[int] = set()
    for tier in ordered:
        if tier.min_units in seen:
            raise TierConfigurationError(
                f"duplicate tier at min_units={tier.min_units}"
            )
        seen.add(tier.min_units)

    for earlier, later in zip(ordered, ordered[1:]):
        if later.unit_price_cents > earlier.unit_price_cents:
            raise TierConfigurationError(
                f"tier at {later.min_units} units is priced "
                f"{later.unit_price_cents}c, above the {earlier.min_units}-unit "
                f"tier at {earlier.unit_price_cents}c; price must not increase "
                f"with quantity"
            )
    return ordered


def resolve_tier(tiers: Sequence[PriceTier], units: int) -> PriceTier:
    """Return the tier that applies at ``units``.

    Below the first tier we return the first (most expensive) tier: that is the
    price shown to early joiners, and the price we authorise against.
    """
    ordered = validate_tiers(tiers)
    applicable = [t for t in ordered if t.min_units <= units]
    return applicable[-1] if applicable else ordered[0]


def resolve_unit_price_cents(tiers: Sequence[PriceTier], units: int) -> int:
    return resolve_tier(tiers, units).unit_price_cents


def next_tier(tiers: Sequence[PriceTier], units: int) -> PriceTier | None:
    """The next cheaper tier, or ``None`` if already at the best price.

    Drives the single highest-converting string in the product:
    *"3 more and everyone pays $12."*
    """
    ordered = validate_tiers(tiers)
    for tier in ordered:
        if tier.min_units > units:
            return tier
    return None


def units_to_next_tier(tiers: Sequence[PriceTier], units: int) -> int | None:
    nxt = next_tier(tiers, units)
    return None if nxt is None else nxt.min_units - units


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostModel:
    """The unit economics of one stop on one delivery run.

    ``line_haul_cost_cents`` is the cost of getting the van from the depot to
    the neighbourhood at all. It is shared across every unit on the run, so we
    amortise it over ``expected_units_per_run`` rather than charging it to a
    single campaign.

    ``stop_cost_cents`` is the marginal cost of the van *stopping* -- the driver
    minute, the parking, the handover. It does not shrink with volume, which is
    precisely why a minimum quantity per stop has to exist.
    """

    unit_cost_cents: int             # what we pay the supplier per unit
    stop_cost_cents: int             # marginal cost of one extra stop
    line_haul_cost_cents: int = 0    # per run, depot -> neighbourhood
    expected_units_per_run: int = 1  # denominator for amortising line haul
    commission_bps: int = 0          # captain's cut of gross
    psp_fee_bps: int = 290           # card processing, percentage part
    psp_fixed_cents: int = 30        # card processing, per-order part
    avg_units_per_order: int = 1     # converts the fixed fee to a per-unit one

    def __post_init__(self) -> None:
        if self.unit_cost_cents <= 0:
            raise ValueError("unit_cost_cents must be positive")
        if self.stop_cost_cents < 0 or self.line_haul_cost_cents < 0:
            raise ValueError("costs must be non-negative")
        if self.expected_units_per_run <= 0:
            raise ValueError("expected_units_per_run must be positive")
        if self.avg_units_per_order <= 0:
            raise ValueError("avg_units_per_order must be positive")
        if not 0 <= self.commission_bps <= BPS:
            raise ValueError("commission_bps out of range")
        if not 0 <= self.psp_fee_bps <= BPS:
            raise ValueError("psp_fee_bps out of range")

    @property
    def line_haul_per_unit_cents(self) -> int:
        """Line haul amortised across a full run, rounded up."""
        return ceil_div(self.line_haul_cost_cents, self.expected_units_per_run)

    @property
    def psp_fixed_per_unit_cents(self) -> int:
        return ceil_div(self.psp_fixed_cents, self.avg_units_per_order)

    def variable_cost_per_unit_cents(self, unit_price_cents: int) -> int:
        """All per-unit costs at a given sale price, rounded up."""
        return (
            self.unit_cost_cents
            + self.line_haul_per_unit_cents
            + self.psp_fixed_per_unit_cents
            + apply_bps(unit_price_cents, self.commission_bps, round_up=True)
            + apply_bps(unit_price_cents, self.psp_fee_bps, round_up=True)
        )

    def contribution_per_unit_cents(self, unit_price_cents: int) -> int:
        """Margin each unit contributes toward the fixed per-stop cost.

        May be negative, which means the item is underwater before the van has
        even stopped -- no threshold can rescue it.
        """
        return unit_price_cents - self.variable_cost_per_unit_cents(unit_price_cents)


# ---------------------------------------------------------------------------
# Threshold derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdAnalysis:
    """Why a threshold is what it is -- shown to ops, not just to the solver."""

    viable: bool
    min_units: int | None                 # case-aligned, MOQ-respecting answer
    raw_break_even_units: int | None      # before case/MOQ adjustments
    contribution_per_unit_cents: int
    fixed_cost_to_cover_cents: int
    binding_constraint: str               # 'economics' | 'case_pack' | 'supplier_moq' | 'none'
    reason: str


def min_viable_units(
    *,
    unit_price_cents: int,
    cost: CostModel,
    target_margin_cents: int = 0,
    case_pack_units: int = 1,
    supplier_min_units: int = 0,
) -> ThresholdAnalysis:
    """Solve for the smallest quantity at which shipping this stop is worth it.

    Contribution at quantity ``Q`` is::

        Q * contribution_per_unit - stop_cost

    We need that to reach ``target_margin_cents``, so::

        Q >= (stop_cost + target_margin) / contribution_per_unit

    then round up to a whole case and lift to the supplier's MOQ. The binding
    constraint is reported so ops can see the lever: if it is ``case_pack`` the
    fix is a smaller case, if it is ``economics`` the fix is price or cost.
    """
    if unit_price_cents <= 0:
        raise ValueError("unit_price_cents must be positive")
    if case_pack_units <= 0:
        raise ValueError("case_pack_units must be positive")
    if supplier_min_units < 0:
        raise ValueError("supplier_min_units must be non-negative")

    contribution = cost.contribution_per_unit_cents(unit_price_cents)
    fixed = cost.stop_cost_cents + target_margin_cents

    if contribution <= 0:
        return ThresholdAnalysis(
            viable=False,
            min_units=None,
            raw_break_even_units=None,
            contribution_per_unit_cents=contribution,
            fixed_cost_to_cover_cents=fixed,
            binding_constraint="economics",
            reason=(
                f"each unit contributes {contribution}c at a price of "
                f"{unit_price_cents}c, so volume cannot cover the "
                f"{cost.stop_cost_cents}c stop cost. Raise the price, cut the "
                f"unit cost, or drop the item."
            ),
        )

    raw = max(1, ceil_div(fixed, contribution)) if fixed > 0 else 1

    case_aligned = round_up_to_multiple(raw, case_pack_units)
    final = max(case_aligned, supplier_min_units)
    if final % case_pack_units:
        final = round_up_to_multiple(final, case_pack_units)

    if final == raw:
        binding = "economics" if fixed > 0 else "none"
    elif final == case_aligned:
        binding = "case_pack"
    else:
        binding = "supplier_moq"

    return ThresholdAnalysis(
        viable=True,
        min_units=final,
        raw_break_even_units=raw,
        contribution_per_unit_cents=contribution,
        fixed_cost_to_cover_cents=fixed,
        binding_constraint=binding,
        reason=(
            f"{final} units: {contribution}c contribution per unit must cover "
            f"{fixed}c of fixed stop cost (break-even {raw}, "
            f"case pack {case_pack_units}, supplier MOQ {supplier_min_units})"
        ),
    )


def campaign_contribution_cents(
    *,
    units: int,
    unit_price_cents: int,
    cost: CostModel,
    purchased_units: int | None = None,
) -> int:
    """Actual contribution of a campaign, charging for unsold case overage.

    ``purchased_units`` is what we buy from the supplier (a whole number of
    cases); ``units`` is what buyers actually pay for. The gap is inventory we
    own and have not sold, and it is charged here at full cost -- optimistic
    overage accounting is how a campaign that looks profitable in the dashboard
    turns out not to be.
    """
    if units < 0:
        raise ValueError("units must be non-negative")
    purchased = units if purchased_units is None else purchased_units
    if purchased < units:
        raise ValueError("purchased_units cannot be below sold units")

    revenue = units * unit_price_cents
    per_unit_variable = cost.variable_cost_per_unit_cents(unit_price_cents)
    sold_cost = units * per_unit_variable
    overage_cost = (purchased - units) * cost.unit_cost_cents
    return revenue - sold_cost - overage_cost - cost.stop_cost_cents


def min_units_per_stop(
    *, unit_price_cents: int, cost: CostModel, case_pack_units: int = 1
) -> int | None:
    """Smallest quantity that justifies adding *one more stop* to a run.

    This is the gate on merging a neighbouring pickup point into a campaign
    (see :mod:`app.domain.matching`). Line haul is already paid by the run, so
    only the marginal stop cost has to be earned back -- which is why a merge
    threshold is lower than a standalone campaign threshold.
    """
    marginal = CostModel(
        unit_cost_cents=cost.unit_cost_cents,
        stop_cost_cents=cost.stop_cost_cents,
        line_haul_cost_cents=0,  # already sunk by the run
        expected_units_per_run=cost.expected_units_per_run,
        commission_bps=cost.commission_bps,
        psp_fee_bps=cost.psp_fee_bps,
        psp_fixed_cents=cost.psp_fixed_cents,
        avg_units_per_order=cost.avg_units_per_order,
    )
    analysis = min_viable_units(
        unit_price_cents=unit_price_cents,
        cost=marginal,
        case_pack_units=case_pack_units,
    )
    return analysis.min_units


def suggest_tier_ladder(
    *,
    cost: CostModel,
    anchor_price_cents: int,
    threshold_units: int,
    steps: Sequence[tuple[int, int]] = ((2, 400), (4, 700)),
) -> list[PriceTier]:
    """Build a tier ladder that discounts only what extra volume actually saves.

    ``steps`` is ``(multiple_of_threshold, discount_bps)``. The guard that
    matters: a tier is only emitted while it still leaves positive contribution,
    so the growth mechanic can never be configured into a loss.
    """
    ladder = [PriceTier(min_units=threshold_units, unit_price_cents=anchor_price_cents)]
    for multiple, discount_bps in steps:
        price = anchor_price_cents - apply_bps(
            anchor_price_cents, discount_bps, round_up=False
        )
        if price <= 0 or cost.contribution_per_unit_cents(price) <= 0:
            break
        ladder.append(
            PriceTier(min_units=threshold_units * multiple, unit_price_cents=price)
        )
    return validate_tiers(ladder)


__all__ = [
    "BPS",
    "CostModel",
    "PriceTier",
    "ThresholdAnalysis",
    "TierConfigurationError",
    "apply_bps",
    "campaign_contribution_cents",
    "ceil_div",
    "min_units_per_stop",
    "min_viable_units",
    "next_tier",
    "resolve_tier",
    "resolve_unit_price_cents",
    "round_up_to_multiple",
    "suggest_tier_ladder",
    "units_to_next_tier",
    "validate_tiers",
]
