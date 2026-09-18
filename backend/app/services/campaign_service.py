"""The imperative shell around the threshold engine.

Every function here follows the same shape:

    1. Take a row lock.
    2. Build an immutable snapshot.
    3. Ask ``app.domain.thresholds`` for a plan (pure).
    4. Apply the plan and append to the outbox.

None of these functions commit. They are designed to run inside a caller's
transaction (the FastAPI dependency in ``app.db.get_db``, or the job runner), so
the row locks taken in step 1 are held until the caller commits -- which is the
entire point.

LOCK ORDERING
-------------
Deadlock avoidance rests on one rule, and it is not negotiable:

    **campaign first, then its orders in ascending id order.**

Any code path that violates it can deadlock against the window-close sweeper,
which locks a campaign and then every order underneath it.

WHY A ROW LOCK AND NOT ``SERIALIZABLE``
---------------------------------------
Threshold decisions read a counter, decide, and write the counter back. Under
SERIALIZABLE that is a write-skew hotspot: every concurrent join to a popular
campaign would abort and retry, and the retry storm peaks exactly when a
campaign is going viral. ``SELECT ... FOR UPDATE`` on the campaign row serialises
the same critical section explicitly, with contention scoped to one campaign and
no retry loop. Campaigns are independent, so this does not serialise the site.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, lazyload

from app.config import Settings, get_settings
from app.domain.pricing import PriceTier
from app.domain.state import (
    COUNTED_ORDER_STATES,
    CampaignState,
    OrderState,
    assert_campaign_transition,
    assert_order_transition,
)
from app.domain.thresholds import (
    CampaignSnapshot,
    CloseAction,
    CloseDecision,
    LockPlan,
    LockPolicy,
    OrderSnapshot,
    plan_close,
    plan_expiry,
    plan_lock,
    plan_reservation,
    should_lock_early,
)
from app.errors import CapacityExhausted, Conflict, NotFound, ValidationFailed
from app.models import (
    Campaign,
    CampaignEvent,
    Order,
    OutboxMessage,
    PaymentIntent,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def policy_from_settings(settings: Settings | None = None) -> LockPolicy:
    s = settings or get_settings()
    return LockPolicy(
        reservation_ttl_seconds=s.reservation_ttl_seconds,
        rescue_ratio_bps=s.rescue_ratio_bps,
        rescue_extension_hours=s.rescue_extension_hours,
        allow_rescue=s.allow_rescue,
    )


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------


def to_snapshot(campaign: Campaign) -> CampaignSnapshot:
    tiers = tuple(
        PriceTier(min_units=t.min_units, unit_price_cents=t.unit_price_cents)
        for t in sorted(campaign.tiers, key=lambda t: t.min_units)
    )
    return CampaignSnapshot(
        campaign_id=str(campaign.id),
        state=campaign.state,
        threshold_units=campaign.threshold_units,
        cap_units=campaign.cap_units,
        case_pack_units=campaign.case_pack_units,
        committed_units=campaign.committed_units,
        reserved_units=campaign.reserved_units,
        opens_at=campaign.opens_at,
        closes_at=campaign.closes_at,
        tiers=tiers,
        extended_until=campaign.extended_until,
        secured_at=campaign.secured_at,
    )


def _lock_campaign(session: Session, campaign_id: uuid.UUID) -> Campaign:
    """Step 1 of every flow. Serialises all mutation of this campaign.

    The eager-loaded ``variant`` and ``pickup_point`` relationships are
    explicitly switched off here. They render as LEFT OUTER JOINs, and
    PostgreSQL rejects ``FOR UPDATE`` against the nullable side of an outer
    join. Dropping them is also the right call on its own terms: this query
    exists to lock a row and mutate counters, not to read catalogue data.
    ``tiers`` uses ``selectin`` loading, so it issues its own statement and is
    unaffected.
    """
    campaign = session.execute(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .options(lazyload(Campaign.variant), lazyload(Campaign.pickup_point))
        .with_for_update()
    ).scalar_one_or_none()
    if campaign is None:
        raise NotFound(f"campaign {campaign_id} not found")
    return campaign


def _load_order_snapshots(session: Session, campaign_id: uuid.UUID) -> list[OrderSnapshot]:
    rows = session.execute(
        select(Order).where(Order.campaign_id == campaign_id).order_by(Order.id)
    ).scalars().all()
    return [
        OrderSnapshot(
            order_id=str(o.id),
            buyer_id=str(o.buyer_id),
            quantity=o.quantity,
            quoted_unit_price_cents=o.quoted_unit_price_cents,
            state=o.state,
        )
        for o in rows
    ]


def _emit(
    session: Session,
    campaign: Campaign,
    event_type: str,
    *,
    from_state: CampaignState | None = None,
    to_state: CampaignState | None = None,
    payload: dict | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    session.add(
        CampaignEvent(
            campaign_id=campaign.id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            payload=payload or {},
            actor_user_id=actor_user_id,
        )
    )


def _outbox(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    payload: dict | None = None,
    available_at: datetime | None = None,
) -> None:
    """Queue a side effect in the same transaction as the state change.

    Nothing that talks to a payment provider, a supplier or a push service may
    be called inline. If the transaction rolls back, the side effect must
    disappear with it.

    ``available_at`` is taken from the caller's injected clock rather than the
    database's ``now()``. Mixing the two would make a message's due time
    disagree with the decision that produced it, which matters for any flow that
    controls its own clock -- backfills, replays and tests alike.
    """
    session.add(
        OutboxMessage(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload or {},
            available_at=available_at or utcnow(),
        )
    )


# ---------------------------------------------------------------------------
# Buyer flows
# ---------------------------------------------------------------------------


def join_campaign(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    buyer_id: uuid.UUID,
    quantity: int,
    idempotency_key: str,
    pickup_point_id: uuid.UUID | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Order:
    """Reserve capacity for a buyer and queue the authorization.

    The order lands in ``pending_auth``: capacity is held, but it does not count
    toward the threshold until the card authorization comes back. That gap is
    what ``reserved_units`` exists for -- without it, a burst of joins on a
    nearly-full campaign would oversell the supplier's cap while everyone waited
    on the PSP.
    """
    now = now or utcnow()
    policy = policy_from_settings(settings)

    if quantity <= 0:
        raise ValidationFailed("quantity must be positive")

    # Idempotency before anything else: a double-tapped button on a flaky mobile
    # connection must return the first order, not create a second.
    existing = session.execute(
        select(Order).where(Order.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    campaign = _lock_campaign(session, campaign_id)
    snapshot = to_snapshot(campaign)

    current = session.execute(
        select(Order).where(
            Order.campaign_id == campaign_id,
            Order.buyer_id == buyer_id,
            Order.state.notin_(
                [OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REFUNDED]
            ),
        )
    ).scalar_one_or_none()
    if current is not None:
        raise Conflict(
            "buyer already has a live order on this campaign; resize it instead"
        )

    plan = plan_reservation(
        snapshot, requested_quantity=quantity, now=now, policy=policy
    )
    if not plan.accepted:
        if plan.rejection and plan.rejection.value == "insufficient_capacity":
            raise CapacityExhausted(plan.message)
        raise Conflict(plan.message, code=plan.rejection.value if plan.rejection else None)

    order = Order(
        campaign_id=campaign_id,
        buyer_id=buyer_id,
        pickup_point_id=pickup_point_id or campaign.pickup_point_id,
        state=OrderState.PENDING_AUTH,
        quantity=quantity,
        quoted_unit_price_cents=plan.quoted_unit_price_cents,
        reserved_until=plan.reserved_until,
        idempotency_key=idempotency_key,
    )
    session.add(order)

    campaign.reserved_units += plan.units_delta

    try:
        session.flush()
    except IntegrityError:
        # Lost a race on the idempotency key or the one-live-order-per-buyer
        # index. Both mean someone else already did this; return their row.
        session.rollback()
        winner = session.execute(
            select(Order).where(Order.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if winner is not None:
            return winner
        raise

    _outbox(
        session,
        aggregate_type="order",
        aggregate_id=order.id,
        event_type="order.authorize_requested",
        payload={
            "amount_cents": plan.authorize_amount_cents,
            "buyer_id": str(buyer_id),
        },
        available_at=now,
    )
    return order


def confirm_authorization(
    session: Session,
    *,
    order_id: uuid.UUID,
    psp_ref: str,
    authorized_amount_cents: int,
    authorization_expires_at: datetime | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Order:
    """Promote a reservation to a real commitment once the card is authorized.

    This is where ``secured_at`` gets stamped and where an at-cap campaign locks
    early. Note the lock order: campaign first, then the order.
    """
    now = now or utcnow()
    policy = policy_from_settings(settings)

    order = session.get(Order, order_id)
    if order is None:
        raise NotFound(f"order {order_id} not found")
    if order.state is OrderState.COMMITTED:
        return order  # idempotent replay of a PSP webhook
    assert_order_transition(order.state, OrderState.COMMITTED)

    campaign = _lock_campaign(session, order.campaign_id)
    # Re-read the order under the campaign lock now that we hold it.
    session.refresh(order)

    snapshot = to_snapshot(campaign)
    advanced = snapshot.with_confirmation(order.quantity, now)

    campaign.reserved_units = max(0, campaign.reserved_units - order.quantity)
    campaign.committed_units += order.quantity
    if campaign.secured_at is None and advanced.secured_at is not None:
        campaign.secured_at = advanced.secured_at
        _emit(
            session,
            campaign,
            "threshold_met",
            payload={"committed_units": campaign.committed_units},
        )
        # Tell everyone already in that the buy is happening. This notification
        # is the single biggest driver of second-order shares.
        _outbox(
            session,
            aggregate_type="campaign",
            aggregate_id=campaign.id,
            event_type="campaign.secured",
            payload={"committed_units": campaign.committed_units},
            available_at=now,
        )

    order.state = OrderState.COMMITTED
    order.reserved_until = None

    intent = session.execute(
        select(PaymentIntent).where(PaymentIntent.order_id == order.id)
    ).scalar_one_or_none()
    if intent is None:
        intent = PaymentIntent(
            order_id=order.id,
            authorized_amount_cents=authorized_amount_cents,
        )
        session.add(intent)
    intent.psp_intent_ref = psp_ref
    intent.state = "authorized"
    intent.authorized_amount_cents = authorized_amount_cents
    intent.authorization_expires_at = authorization_expires_at

    _emit(
        session,
        campaign,
        "order_committed",
        payload={"order_id": str(order.id), "quantity": order.quantity},
    )

    # No upside left in waiting once the cap is reached.
    if should_lock_early(to_snapshot(campaign), policy):
        lock_campaign(session, campaign_id=campaign.id, now=now, _campaign=campaign)

    return order


def withdraw_order(
    session: Session,
    *,
    order_id: uuid.UUID,
    now: datetime | None = None,
) -> Order:
    """Let a buyer back out before the campaign locks.

    Free until lock, impossible after: once locked we have captured money and
    cut a supplier PO against their unit. Making that boundary obvious in the UI
    is what keeps support volume down.
    """
    now = now or utcnow()

    order = session.get(Order, order_id)
    if order is None:
        raise NotFound(f"order {order_id} not found")

    campaign = _lock_campaign(session, order.campaign_id)
    session.refresh(order)

    if order.state not in (OrderState.PENDING_AUTH, OrderState.COMMITTED):
        raise Conflict(
            f"order is {order.state} and can no longer be withdrawn"
        )
    assert_order_transition(order.state, OrderState.CANCELLED)

    if order.state is OrderState.PENDING_AUTH:
        campaign.reserved_units = max(0, campaign.reserved_units - order.quantity)
    else:
        campaign.committed_units = max(0, campaign.committed_units - order.quantity)
        # Dropping back below threshold un-secures the campaign: buyers must not
        # be told it is happening when it no longer is.
        if campaign.committed_units < campaign.threshold_units:
            campaign.secured_at = None

    order.state = OrderState.CANCELLED
    order.reserved_until = None

    _outbox(
        session,
        aggregate_type="order",
        aggregate_id=order.id,
        event_type="order.void_requested",
        payload={},
        available_at=now,
    )
    _emit(
        session,
        campaign,
        "order_withdrawn",
        payload={"order_id": str(order.id), "quantity": order.quantity},
    )
    return order


# ---------------------------------------------------------------------------
# Lifecycle flows
# ---------------------------------------------------------------------------


def lock_campaign(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    now: datetime | None = None,
    _campaign: Campaign | None = None,
) -> LockPlan:
    """Commit the buy: fix the final price, capture money, cut the PO.

    Everything monetary is queued through the outbox rather than executed here.
    A campaign with 300 orders produces 300 capture jobs; doing them inline
    would hold the campaign row lock across 300 network calls and block every
    other reader of the campaign for the duration.
    """
    now = now or utcnow()
    campaign = _campaign or _lock_campaign(session, campaign_id)

    if campaign.state is not CampaignState.OPEN:
        raise Conflict(f"campaign is {campaign.state}, cannot lock")

    snapshot = to_snapshot(campaign)
    orders = _load_order_snapshots(session, campaign.id)
    plan = plan_lock(snapshot, orders, now=now)

    assert_campaign_transition(campaign.state, CampaignState.LOCKED)
    previous_state = campaign.state
    campaign.state = CampaignState.LOCKED
    campaign.locked_at = now
    campaign.locked_unit_price_cents = plan.final_unit_price_cents
    campaign.final_unit_price_cents = plan.final_unit_price_cents
    campaign.version += 1

    settlement_by_id = {s.order_id: s for s in plan.settlements}
    order_rows = session.execute(
        select(Order)
        .where(
            Order.campaign_id == campaign.id,
            Order.state.in_(list(COUNTED_ORDER_STATES)),
        )
        .order_by(Order.id)            # lock order: campaign, then orders by id
        .with_for_update()
    ).scalars().all()

    for row in order_rows:
        settlement = settlement_by_id.get(str(row.id))
        if settlement is None:
            continue
        assert_order_transition(row.state, OrderState.LOCKED)
        row.state = OrderState.LOCKED
        row.charged_unit_price_cents = settlement.final_unit_price_cents
        _outbox(
            session,
            aggregate_type="order",
            aggregate_id=row.id,
            event_type="order.capture_requested",
            payload={
                "amount_cents": settlement.capture_amount_cents,
                "final_unit_price_cents": settlement.final_unit_price_cents,
                "price_improvement_cents": settlement.price_improvement_cents,
            },
            available_at=now,
        )

    _emit(
        session,
        campaign,
        "campaign_locked",
        from_state=previous_state,
        to_state=CampaignState.LOCKED,
        payload={
            "sold_units": plan.sold_units,
            "purchase_units": plan.purchase_units,
            "overage_units": plan.overage_units,
            "final_unit_price_cents": plan.final_unit_price_cents,
            "total_capture_cents": plan.total_capture_cents,
            "total_price_improvement_cents": plan.total_price_improvement_cents,
            "warnings": list(plan.warnings),
        },
    )
    _outbox(
        session,
        aggregate_type="campaign",
        aggregate_id=campaign.id,
        event_type="campaign.locked",
        payload={
            "purchase_units": plan.purchase_units,
            "final_unit_price_cents": plan.final_unit_price_cents,
        },
        available_at=now,
    )
    return plan


def expire_campaign(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    now: datetime | None = None,
    _campaign: Campaign | None = None,
) -> None:
    """Kill a campaign that missed its threshold and release every hold.

    No money was captured, so nothing is refunded -- the holds are voided. The
    notification wording matters commercially: buyers who understand they were
    never charged come back for the next campaign.
    """
    now = now or utcnow()
    campaign = _campaign or _lock_campaign(session, campaign_id)

    if campaign.state is not CampaignState.OPEN:
        raise Conflict(f"campaign is {campaign.state}, cannot expire")

    snapshot = to_snapshot(campaign)
    orders = _load_order_snapshots(session, campaign.id)
    plan = plan_expiry(snapshot, orders)

    assert_campaign_transition(campaign.state, CampaignState.EXPIRED)
    campaign.state = CampaignState.EXPIRED
    campaign.version += 1

    rows = session.execute(
        select(Order)
        .where(
            Order.campaign_id == campaign.id,
            Order.state.in_([OrderState.PENDING_AUTH, OrderState.COMMITTED]),
        )
        .order_by(Order.id)
        .with_for_update()
    ).scalars().all()

    for row in rows:
        assert_order_transition(row.state, OrderState.EXPIRED)
        row.state = OrderState.EXPIRED
        row.reserved_until = None
        _outbox(
            session,
            aggregate_type="order",
            aggregate_id=row.id,
            event_type="order.void_requested",
            payload={"reason": "campaign_expired"},
            available_at=now,
        )

    campaign.committed_units = 0
    campaign.reserved_units = 0
    campaign.secured_at = None

    _emit(
        session,
        campaign,
        "campaign_expired",
        from_state=CampaignState.OPEN,
        to_state=CampaignState.EXPIRED,
        payload={
            "shortfall_units": plan.shortfall_units,
            "voided_orders": len(plan.void_order_ids),
            "total_voided_cents": plan.total_voided_cents,
        },
    )
    _outbox(
        session,
        aggregate_type="campaign",
        aggregate_id=campaign.id,
        event_type="campaign.expired",
        payload={"shortfall_units": plan.shortfall_units},
        available_at=now,
    )


def close_campaign(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> CloseDecision:
    """Run the window-close decision and carry it out.

    Called by the sweeper for every campaign past its deadline. Safe to call
    repeatedly: a campaign that is not due, or not open, returns ``NOOP``.
    """
    now = now or utcnow()
    policy = policy_from_settings(settings)

    campaign = _lock_campaign(session, campaign_id)
    snapshot = to_snapshot(campaign)
    decision = plan_close(snapshot, now=now, policy=policy)

    if decision.action is CloseAction.LOCK:
        lock_campaign(session, campaign_id=campaign.id, now=now, _campaign=campaign)
    elif decision.action is CloseAction.EXPIRE:
        expire_campaign(session, campaign_id=campaign.id, now=now, _campaign=campaign)
    elif decision.action is CloseAction.EXTEND:
        campaign.extended_until = decision.new_close_at
        campaign.version += 1
        _emit(
            session,
            campaign,
            "campaign_extended",
            payload={
                "new_close_at": decision.new_close_at.isoformat()
                if decision.new_close_at
                else None,
                "shortfall_units": decision.shortfall_units,
            },
        )
        _outbox(
            session,
            aggregate_type="campaign",
            aggregate_id=campaign.id,
            event_type="campaign.extended",
            payload={"shortfall_units": decision.shortfall_units},
            available_at=now,
        )

    return decision


def sweep_expired_reservations(
    session: Session, *, now: datetime | None = None, limit: int = 500
) -> int:
    """Return capacity held by authorizations that never came back.

    Without this, an abandoned checkout on a nearly-full campaign permanently
    blocks a slot that a real buyer would have taken.
    """
    now = now or utcnow()

    stale = session.execute(
        select(Order)
        .where(
            Order.state == OrderState.PENDING_AUTH,
            Order.reserved_until < now,
        )
        .order_by(Order.reserved_until)
        .limit(limit)
    ).scalars().all()

    released = 0
    for order in stale:
        campaign = _lock_campaign(session, order.campaign_id)
        session.refresh(order)
        if order.state is not OrderState.PENDING_AUTH:
            continue  # someone confirmed it while we were acquiring the lock
        campaign.reserved_units = max(0, campaign.reserved_units - order.quantity)
        order.state = OrderState.EXPIRED
        order.reserved_until = None
        released += order.quantity
        _emit(
            session,
            campaign,
            "reservation_expired",
            payload={"order_id": str(order.id), "quantity": order.quantity},
        )
    return released


__all__ = [
    "close_campaign",
    "confirm_authorization",
    "expire_campaign",
    "join_campaign",
    "lock_campaign",
    "policy_from_settings",
    "sweep_expired_reservations",
    "to_snapshot",
    "utcnow",
    "withdraw_order",
]
