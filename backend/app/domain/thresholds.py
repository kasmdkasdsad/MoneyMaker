"""The threshold-locking engine.

This is the functional core of the platform: a set of **pure decision
functions** over an immutable snapshot of a campaign. They allocate no
resources, touch no database and call no payment provider. They take the
current facts and return a *plan* -- and the imperative shell in
``app/services/campaign_service.py`` executes that plan inside one transaction.

Why split it this way:

* The interesting failure modes here are concurrency and money. Both are far
  easier to reason about, and to test exhaustively, when the decision is
  separable from the I/O that carries it out.
* Replaying a plan is safe. If the shell crashes mid-execution it re-derives
  the plan from the persisted snapshot and continues, which is what makes
  "capture 200 payments" restartable.

THE CENTRAL PRODUCT DECISION ENCODED HERE
-----------------------------------------
Hitting the threshold does **not** immediately lock the campaign. Crossing the
threshold marks it *secured* -- buyers are promised it will ship -- but the
campaign stays open until its window closes, so late joiners keep pushing
everyone down the price tiers. Locking early would trade away the entire growth
loop for a few hours of operational lead time.

The one exception is hitting the supplier/host cap: at that point there is no
more upside to wait for, so we lock immediately and get a head start on
fulfilment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.pricing import (
    PriceTier,
    resolve_unit_price_cents,
    round_up_to_multiple,
    units_to_next_tier,
    validate_tiers,
)
from app.domain.state import COUNTED_ORDER_STATES, CampaignState, OrderState

# ---------------------------------------------------------------------------
# Snapshots (inputs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderSnapshot:
    order_id: str
    buyer_id: str
    quantity: int
    quoted_unit_price_cents: int
    state: OrderState

    @property
    def counts_toward_threshold(self) -> bool:
        return self.state in COUNTED_ORDER_STATES


@dataclass(frozen=True)
class CampaignSnapshot:
    """Everything the engine needs to decide, and nothing else."""

    campaign_id: str
    state: CampaignState
    threshold_units: int
    cap_units: int
    case_pack_units: int
    committed_units: int
    reserved_units: int
    opens_at: datetime
    closes_at: datetime
    tiers: tuple[PriceTier, ...]
    extended_until: datetime | None = None
    secured_at: datetime | None = None
    supplier_min_units: int = 0

    def __post_init__(self) -> None:
        if self.threshold_units <= 0:
            raise ValueError("threshold_units must be positive")
        if self.cap_units < self.threshold_units:
            raise ValueError("cap_units must be >= threshold_units")
        if self.case_pack_units <= 0:
            raise ValueError("case_pack_units must be positive")
        if self.committed_units < 0 or self.reserved_units < 0:
            raise ValueError("counters must be non-negative")
        if self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be after opens_at")
        validate_tiers(self.tiers)

    # --- derived facts ----------------------------------------------------

    @property
    def effective_close_at(self) -> datetime:
        """Window end, accounting for a rescue extension."""
        return self.extended_until or self.closes_at

    @property
    def held_units(self) -> int:
        """Units that are spoken for: confirmed plus in-flight authorizations."""
        return self.committed_units + self.reserved_units

    @property
    def available_units(self) -> int:
        """Units a new buyer could still claim. Never negative."""
        return max(0, self.cap_units - self.held_units)

    @property
    def is_secured(self) -> bool:
        """Threshold met -- this campaign is going to ship."""
        return self.committed_units >= self.threshold_units

    @property
    def units_to_threshold(self) -> int:
        return max(0, self.threshold_units - self.committed_units)

    @property
    def progress_bps(self) -> int:
        """Progress toward threshold in basis points, capped at 100%."""
        return min(10_000, (self.committed_units * 10_000) // self.threshold_units)

    @property
    def current_unit_price_cents(self) -> int:
        return resolve_unit_price_cents(self.tiers, self.committed_units)

    @property
    def units_to_next_tier(self) -> int | None:
        return units_to_next_tier(self.tiers, self.committed_units)

    def is_window_open(self, now: datetime) -> bool:
        return self.opens_at <= now < self.effective_close_at

    def is_due_for_close(self, now: datetime) -> bool:
        return now >= self.effective_close_at

    # --- immutable state advancement (used by the shell and by simulations) --

    def with_reservation(self, units: int) -> "CampaignSnapshot":
        return replace(self, reserved_units=self.reserved_units + units)

    def with_confirmation(self, units: int, now: datetime) -> "CampaignSnapshot":
        """Move units from reserved to committed, stamping ``secured_at`` on the
        first crossing of the threshold."""
        committed = self.committed_units + units
        secured_at = self.secured_at
        if secured_at is None and committed >= self.threshold_units:
            secured_at = now
        return replace(
            self,
            reserved_units=max(0, self.reserved_units - units),
            committed_units=committed,
            secured_at=secured_at,
        )

    def with_release(self, units: int) -> "CampaignSnapshot":
        return replace(self, reserved_units=max(0, self.reserved_units - units))

    def with_withdrawal(self, units: int) -> "CampaignSnapshot":
        return replace(
            self, committed_units=max(0, self.committed_units - units)
        )


@dataclass(frozen=True)
class LockPolicy:
    """Tunable knobs. Defaults are the launch configuration."""

    # How long a buyer's capacity reservation survives while we talk to the PSP.
    reservation_ttl_seconds: int = 180
    # A campaign that closes at or above this fraction of threshold gets one
    # extension instead of dying. Near-misses are the cheapest demand we will
    # ever see -- the buyers are already in the cart.
    rescue_ratio_bps: int = 8_000  # 80%
    rescue_extension_hours: int = 24
    allow_rescue: bool = True
    max_extensions: int = 1
    # Lock the moment the cap is reached; there is no further upside to waiting.
    lock_early_at_cap: bool = True

    def __post_init__(self) -> None:
        if self.reservation_ttl_seconds <= 0:
            raise ValueError("reservation_ttl_seconds must be positive")
        if not 0 <= self.rescue_ratio_bps <= 10_000:
            raise ValueError("rescue_ratio_bps out of range")
        if self.rescue_extension_hours <= 0:
            raise ValueError("rescue_extension_hours must be positive")


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class RejectionCode(StrEnum):
    NOT_OPEN = "campaign_not_open"
    WINDOW_CLOSED = "window_closed"
    WINDOW_NOT_STARTED = "window_not_started"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    INVALID_QUANTITY = "invalid_quantity"


@dataclass(frozen=True)
class ReservationPlan:
    """Outcome of a buyer trying to join (or resize their spot in) a campaign."""

    accepted: bool
    units_delta: int                   # +N claims capacity, -N returns it
    quoted_unit_price_cents: int
    authorize_amount_cents: int
    reserved_until: datetime | None
    available_units: int               # for "only 3 left" messaging
    would_secure: bool                 # this commitment crosses the threshold
    rejection: RejectionCode | None = None
    message: str = ""


class CloseAction(StrEnum):
    NOOP = "noop"          # not due yet
    LOCK = "lock"          # threshold met -> commit the order
    EXTEND = "extend"      # near miss -> one rescue window
    EXPIRE = "expire"      # missed -> void every authorization


@dataclass(frozen=True)
class CloseDecision:
    action: CloseAction
    new_close_at: datetime | None = None
    committed_units: int = 0
    threshold_units: int = 0
    shortfall_units: int = 0
    reason: str = ""


@dataclass(frozen=True)
class OrderSettlement:
    """Per-order money instructions produced at lock."""

    order_id: str
    quantity: int
    quoted_unit_price_cents: int
    final_unit_price_cents: int
    capture_amount_cents: int
    # Buyers who joined early quoted a worse tier. Later volume earned them a
    # discount, so we capture the lower amount and never charge the difference.
    price_improvement_cents: int


@dataclass(frozen=True)
class LockPlan:
    """The complete, replayable set of effects of locking a campaign."""

    campaign_id: str
    sold_units: int
    purchase_units: int                # rounded up to whole cases
    overage_units: int                 # bought but unsold -- our inventory risk
    final_unit_price_cents: int
    settlements: tuple[OrderSettlement, ...]
    total_capture_cents: int
    total_price_improvement_cents: int
    overage_exceeds_cap: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpiryPlan:
    """What to undo when a campaign misses its threshold."""

    campaign_id: str
    committed_units: int
    shortfall_units: int
    void_order_ids: tuple[str, ...]
    total_voided_cents: int


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def plan_reservation(
    snapshot: CampaignSnapshot,
    *,
    requested_quantity: int,
    now: datetime,
    current_quantity: int = 0,
    policy: LockPolicy | None = None,
) -> ReservationPlan:
    """Decide whether a buyer may claim ``requested_quantity`` units.

    Handles both joining (``current_quantity == 0``) and resizing an existing
    order, because the capacity maths is identical -- only the delta differs.

    The quoted price is the price at the campaign's *current* volume, which is
    the worst price the buyer can end up paying: tiers only ever improve as more
    neighbours join. We authorise that worst case and capture less at lock. That
    ordering is what lets us promise "you will never pay more than this" without
    re-prompting anyone for payment.
    """
    policy = policy or LockPolicy()

    def reject(code: RejectionCode, message: str) -> ReservationPlan:
        return ReservationPlan(
            accepted=False,
            units_delta=0,
            quoted_unit_price_cents=snapshot.current_unit_price_cents,
            authorize_amount_cents=0,
            reserved_until=None,
            available_units=snapshot.available_units,
            would_secure=False,
            rejection=code,
            message=message,
        )

    if requested_quantity < 0 or current_quantity < 0:
        return reject(RejectionCode.INVALID_QUANTITY, "quantity cannot be negative")
    if requested_quantity == current_quantity:
        return reject(RejectionCode.INVALID_QUANTITY, "quantity unchanged")

    if snapshot.state is not CampaignState.OPEN:
        return reject(
            RejectionCode.NOT_OPEN,
            f"campaign is {snapshot.state}, not accepting commitments",
        )
    if now < snapshot.opens_at:
        return reject(RejectionCode.WINDOW_NOT_STARTED, "campaign has not opened yet")
    if snapshot.is_due_for_close(now):
        return reject(RejectionCode.WINDOW_CLOSED, "campaign window has closed")

    delta = requested_quantity - current_quantity

    # Reducing or withdrawing always succeeds: it returns capacity to the pool.
    if delta < 0:
        return ReservationPlan(
            accepted=True,
            units_delta=delta,
            quoted_unit_price_cents=snapshot.current_unit_price_cents,
            authorize_amount_cents=requested_quantity
            * snapshot.current_unit_price_cents,
            reserved_until=None,
            available_units=snapshot.available_units - delta,
            would_secure=False,
            message=f"released {-delta} units",
        )

    if delta > snapshot.available_units:
        return reject(
            RejectionCode.INSUFFICIENT_CAPACITY,
            f"only {snapshot.available_units} units left in this campaign",
        )

    quoted = snapshot.current_unit_price_cents
    return ReservationPlan(
        accepted=True,
        units_delta=delta,
        quoted_unit_price_cents=quoted,
        # Authorise the full new basket, not the delta: the PSP intent covers
        # the whole order, and re-authorising is how we handle resizes.
        authorize_amount_cents=requested_quantity * quoted,
        reserved_until=now + timedelta(seconds=policy.reservation_ttl_seconds),
        available_units=snapshot.available_units - delta,
        would_secure=(
            not snapshot.is_secured
            and snapshot.committed_units + delta >= snapshot.threshold_units
        ),
        message=f"reserved {delta} units at {quoted}c",
    )


def should_lock_early(
    snapshot: CampaignSnapshot, policy: LockPolicy | None = None
) -> bool:
    """True when the cap is reached and there is nothing left to gain by waiting."""
    policy = policy or LockPolicy()
    return (
        policy.lock_early_at_cap
        and snapshot.state is CampaignState.OPEN
        and snapshot.committed_units >= snapshot.cap_units
    )


def plan_close(
    snapshot: CampaignSnapshot,
    *,
    now: datetime,
    policy: LockPolicy | None = None,
) -> CloseDecision:
    """Decide what happens to a campaign whose window has run out.

    Three outcomes, in priority order: lock it, rescue it, or kill it.
    """
    policy = policy or LockPolicy()

    if snapshot.state is not CampaignState.OPEN:
        return CloseDecision(
            action=CloseAction.NOOP,
            committed_units=snapshot.committed_units,
            threshold_units=snapshot.threshold_units,
            reason=f"campaign is {snapshot.state}, nothing to close",
        )

    early = should_lock_early(snapshot, policy)
    if not snapshot.is_due_for_close(now) and not early:
        return CloseDecision(
            action=CloseAction.NOOP,
            committed_units=snapshot.committed_units,
            threshold_units=snapshot.threshold_units,
            reason="window still open",
        )

    if snapshot.is_secured:
        return CloseDecision(
            action=CloseAction.LOCK,
            committed_units=snapshot.committed_units,
            threshold_units=snapshot.threshold_units,
            reason=(
                "cap reached, locking early"
                if early and not snapshot.is_due_for_close(now)
                else f"threshold met with {snapshot.committed_units} units"
            ),
        )

    # Near miss. One extension, and only one -- a campaign that can be extended
    # indefinitely teaches buyers that deadlines are fake, and deadlines are the
    # only reason anyone shares the link today rather than next week.
    extensions_used = 1 if snapshot.extended_until else 0
    near_miss = (
        snapshot.committed_units * 10_000
        >= snapshot.threshold_units * policy.rescue_ratio_bps
    )
    if (
        policy.allow_rescue
        and near_miss
        and extensions_used < policy.max_extensions
        and snapshot.committed_units > 0
    ):
        return CloseDecision(
            action=CloseAction.EXTEND,
            new_close_at=now + timedelta(hours=policy.rescue_extension_hours),
            committed_units=snapshot.committed_units,
            threshold_units=snapshot.threshold_units,
            shortfall_units=snapshot.units_to_threshold,
            reason=(
                f"{snapshot.committed_units}/{snapshot.threshold_units} units "
                f"({snapshot.progress_bps / 100:.0f}%) -- extending "
                f"{policy.rescue_extension_hours}h to close the gap"
            ),
        )

    return CloseDecision(
        action=CloseAction.EXPIRE,
        committed_units=snapshot.committed_units,
        threshold_units=snapshot.threshold_units,
        shortfall_units=snapshot.units_to_threshold,
        reason=(
            f"closed {snapshot.units_to_threshold} units short of "
            f"{snapshot.threshold_units}"
        ),
    )


def plan_lock(
    snapshot: CampaignSnapshot,
    orders: list[OrderSnapshot],
    *,
    now: datetime,
) -> LockPlan:
    """Compute every money movement required to lock a campaign.

    Deterministic and idempotent: given the same snapshot and orders it returns
    the same plan, so a worker that dies halfway through capturing can simply
    re-derive it and finish the job.
    """
    if not snapshot.is_secured:
        raise ValueError(
            f"cannot lock below threshold: {snapshot.committed_units} < "
            f"{snapshot.threshold_units}"
        )

    counted = [o for o in orders if o.counts_toward_threshold]
    sold_units = sum(o.quantity for o in counted)

    warnings: list[str] = []
    if sold_units != snapshot.committed_units:
        # Counter drift. We trust the orders (the source of truth) and price off
        # them, but this is an alert-worthy integrity problem.
        warnings.append(
            f"counter drift: campaign.committed_units={snapshot.committed_units} "
            f"but orders sum to {sold_units}; pricing off the orders"
        )

    final_price = resolve_unit_price_cents(snapshot.tiers, sold_units)

    purchase_units = round_up_to_multiple(sold_units, snapshot.case_pack_units)
    overage_units = purchase_units - sold_units
    if overage_units:
        warnings.append(
            f"{overage_units} units of case overage to absorb "
            f"(case pack {snapshot.case_pack_units})"
        )

    settlements: list[OrderSettlement] = []
    total_capture = 0
    total_improvement = 0
    for order in sorted(counted, key=lambda o: o.order_id):
        # Never charge more than quoted, even if counter drift produced a worse
        # tier than the buyer saw. The buyer's quote is a hard ceiling.
        charge_price = min(final_price, order.quoted_unit_price_cents)
        capture = order.quantity * charge_price
        improvement = order.quantity * (
            order.quoted_unit_price_cents - charge_price
        )
        settlements.append(
            OrderSettlement(
                order_id=order.order_id,
                quantity=order.quantity,
                quoted_unit_price_cents=order.quoted_unit_price_cents,
                final_unit_price_cents=charge_price,
                capture_amount_cents=capture,
                price_improvement_cents=improvement,
            )
        )
        total_capture += capture
        total_improvement += improvement

    return LockPlan(
        campaign_id=snapshot.campaign_id,
        sold_units=sold_units,
        purchase_units=purchase_units,
        overage_units=overage_units,
        final_unit_price_cents=final_price,
        settlements=tuple(settlements),
        total_capture_cents=total_capture,
        total_price_improvement_cents=total_improvement,
        overage_exceeds_cap=purchase_units > snapshot.cap_units,
        warnings=tuple(warnings),
    )


def plan_expiry(
    snapshot: CampaignSnapshot, orders: list[OrderSnapshot]
) -> ExpiryPlan:
    """Void every authorization on a campaign that missed its threshold.

    No money was ever captured, so there is nothing to refund -- we release the
    holds. Communicating this clearly ("you were never charged") is the
    difference between a failed campaign and a lost customer.
    """
    releasable = [
        o
        for o in orders
        if o.state in (OrderState.PENDING_AUTH, OrderState.COMMITTED)
    ]
    return ExpiryPlan(
        campaign_id=snapshot.campaign_id,
        committed_units=snapshot.committed_units,
        shortfall_units=snapshot.units_to_threshold,
        void_order_ids=tuple(sorted(o.order_id for o in releasable)),
        total_voided_cents=sum(
            o.quantity * o.quoted_unit_price_cents for o in releasable
        ),
    )


__all__ = [
    "CampaignSnapshot",
    "CloseAction",
    "CloseDecision",
    "ExpiryPlan",
    "LockPlan",
    "LockPolicy",
    "OrderSettlement",
    "OrderSnapshot",
    "RejectionCode",
    "ReservationPlan",
    "plan_close",
    "plan_expiry",
    "plan_lock",
    "plan_reservation",
    "should_lock_early",
]
