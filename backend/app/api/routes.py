"""HTTP routes.

Endpoint set is deliberately small: it is exactly the surface the mobile flow in
``docs/03-mobile-flow.md`` needs, plus the ops endpoints that keep thresholds
honest. Anything not on a screen is not here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user_id, idempotency_key, settings_dep
from app.config import Settings
from app.db import get_db
from app.domain.matching import GeoPoint, PickupPointInfo, rank_pickup_points
from app.domain.pricing import (
    CostModel,
    min_viable_units,
    suggest_tier_ladder,
)
from app.domain.state import CampaignState, OrderState
from app.errors import NotFound
from app.models import Campaign, Order, PickupPoint
from app.schemas import (
    CampaignDetailOut,
    CampaignSummaryOut,
    CloseDecisionOut,
    ConfirmAuthorizationIn,
    JoinCampaignIn,
    OrderOut,
    PickupPointOut,
    ThresholdPreviewIn,
    ThresholdPreviewOut,
    TierOut,
)
from app.services import campaign_service

router = APIRouter(prefix="/v1")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _summary(campaign: Campaign, *, distance_m: float | None = None) -> dict:
    snapshot = campaign_service.to_snapshot(campaign)
    nxt = snapshot.units_to_next_tier
    next_price = None
    if nxt is not None:
        target = snapshot.committed_units + nxt
        from app.domain.pricing import resolve_unit_price_cents

        next_price = resolve_unit_price_cents(snapshot.tiers, target)

    variant = campaign.variant
    return {
        "id": campaign.id,
        "title": variant.product.title,
        "image_url": None,
        "state": campaign.state.value,
        "unit_label": variant.unit_label,
        "current_unit_price_cents": snapshot.current_unit_price_cents,
        "list_price_cents": campaign.list_price_cents,
        "threshold_units": snapshot.threshold_units,
        "committed_units": snapshot.committed_units,
        "available_units": snapshot.available_units,
        "units_to_threshold": snapshot.units_to_threshold,
        "progress_bps": snapshot.progress_bps,
        "is_secured": snapshot.is_secured,
        "units_to_next_tier": nxt,
        "next_tier_price_cents": next_price,
        "closes_at": snapshot.effective_close_at,
        "pickup_point_label": campaign.pickup_point.label,
        "pickup_distance_m": distance_m,
    }


def _order_out(order: Order) -> dict:
    return {
        "id": order.id,
        "campaign_id": order.campaign_id,
        "state": order.state.value,
        "quantity": order.quantity,
        "quoted_unit_price_cents": order.quoted_unit_price_cents,
        "charged_unit_price_cents": order.charged_unit_price_cents,
        "total_quoted_cents": order.quantity * order.quoted_unit_price_cents,
        "reserved_until": order.reserved_until,
        "pickup_point_id": order.pickup_point_id,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@router.get("/feed", response_model=list[CampaignSummaryOut])
def get_feed(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_m: float = Query(default=2000, ge=100, le=20_000),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[dict]:
    """The home screen: open campaigns the buyer can actually collect from.

    Ordered by how close each one is to its threshold, not by recency. A
    campaign at 90% converts far better than a fresh one, because joining it
    feels like finishing something rather than starting something.
    """
    buyer = GeoPoint(lat=lat, lon=lon)

    campaigns = db.execute(
        select(Campaign).where(Campaign.state == CampaignState.OPEN)
    ).scalars().all()

    results: list[tuple[float, dict]] = []
    for campaign in campaigns:
        point = campaign.pickup_point
        from app.domain.matching import haversine_m

        distance = haversine_m(buyer, GeoPoint(lat=point.lat, lon=point.lon))
        if distance > radius_m:
            continue
        summary = _summary(campaign, distance_m=distance)
        if summary["available_units"] <= 0:
            continue
        results.append((-summary["progress_bps"], summary))

    results.sort(key=lambda pair: (pair[0], pair[1]["closes_at"]))
    return [summary for _, summary in results[:limit]]


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign(campaign_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound(f"campaign {campaign_id} not found")

    detail = _summary(campaign)
    detail.update(
        {
            "description": campaign.variant.product.description,
            "tiers": [
                TierOut(min_units=t.min_units, unit_price_cents=t.unit_price_cents)
                for t in sorted(campaign.tiers, key=lambda t: t.min_units)
            ],
            "pickup_address_line": campaign.pickup_point.address_line,
            "delivery_eta": campaign.delivery_eta,
            "case_pack_units": campaign.case_pack_units,
        }
    )
    return detail


@router.get("/pickup-points/nearby", response_model=list[PickupPointOut])
def nearby_pickup_points(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> list[dict]:
    points = db.execute(
        select(PickupPoint).where(PickupPoint.is_active.is_(True))
    ).scalars().all()

    live_campaign_point_ids = set(
        db.execute(
            select(Campaign.pickup_point_id).where(
                Campaign.state == CampaignState.OPEN
            )
        ).scalars().all()
    )

    by_id = {str(p.id): p for p in points}
    ranked = rank_pickup_points(
        GeoPoint(lat=lat, lon=lon),
        [
            PickupPointInfo(
                pickup_point_id=str(p.id),
                location=GeoPoint(lat=p.lat, lon=p.lon),
                capacity_units=p.capacity_units,
                has_live_campaign=p.id in live_campaign_point_ids,
            )
            for p in points
        ],
        max_radius_m=settings.pickup_search_radius_m,
        consolidation_bonus_m=settings.consolidation_bonus_m,
    )

    out = []
    for r in ranked:
        point = by_id[r.pickup_point_id]
        out.append(
            {
                "id": point.id,
                "label": point.label,
                "address_line": point.address_line,
                "distance_m": r.distance_m,
                "remaining_capacity": r.remaining_capacity,
                "has_live_campaign": r.has_live_campaign,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Buyer actions
# ---------------------------------------------------------------------------


@router.post("/campaigns/{campaign_id}/join", response_model=OrderOut, status_code=201)
def join_campaign(
    campaign_id: uuid.UUID,
    body: JoinCampaignIn,
    # Identity and idempotency resolve first: FastAPI solves dependencies in
    # signature order and stops at the first failure, so an unauthenticated
    # request never opens a database connection.
    buyer_id: uuid.UUID = Depends(current_user_id),
    key: str = Depends(idempotency_key),
    db: Session = Depends(get_db),
) -> dict:
    order = campaign_service.join_campaign(
        db,
        campaign_id=campaign_id,
        buyer_id=buyer_id,
        quantity=body.quantity,
        idempotency_key=key,
        pickup_point_id=body.pickup_point_id,
    )
    return _order_out(order)


@router.post("/orders/{order_id}/confirm", response_model=OrderOut)
def confirm_order(
    order_id: uuid.UUID,
    body: ConfirmAuthorizationIn,
    db: Session = Depends(get_db),
) -> dict:
    """Called by the PSP webhook once the hold lands.

    PRODUCTION: verify the provider's webhook signature before this runs. Until
    then this endpoint must not be exposed publicly.
    """
    order = campaign_service.confirm_authorization(
        db,
        order_id=order_id,
        psp_ref=body.psp_ref,
        authorized_amount_cents=body.authorized_amount_cents,
        authorization_expires_at=body.authorization_expires_at,
    )
    return _order_out(order)


@router.delete("/orders/{order_id}", response_model=OrderOut)
def withdraw_order(
    order_id: uuid.UUID,
    buyer_id: uuid.UUID = Depends(current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFound(f"order {order_id} not found")
    if order.buyer_id != buyer_id:
        raise NotFound(f"order {order_id} not found")  # do not leak existence
    return _order_out(campaign_service.withdraw_order(db, order_id=order_id))


@router.get("/me/orders", response_model=list[OrderOut])
def my_orders(
    buyer_id: uuid.UUID = Depends(current_user_id),
    include_finished: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[dict]:
    stmt = select(Order).where(Order.buyer_id == buyer_id)
    if not include_finished:
        stmt = stmt.where(
            Order.state.notin_(
                [OrderState.CANCELLED, OrderState.EXPIRED, OrderState.PICKED_UP]
            )
        )
    orders = db.execute(stmt.order_by(Order.created_at.desc())).scalars().all()
    return [_order_out(o) for o in orders]


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------


@router.post("/ops/campaigns/{campaign_id}/close", response_model=CloseDecisionOut)
def close_campaign(campaign_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """Force the window-close decision. Idempotent."""
    decision = campaign_service.close_campaign(db, campaign_id=campaign_id)
    return {
        "action": decision.action.value,
        "reason": decision.reason,
        "committed_units": decision.committed_units,
        "threshold_units": decision.threshold_units,
        "shortfall_units": decision.shortfall_units,
        "new_close_at": decision.new_close_at,
    }


@router.post("/ops/threshold-preview", response_model=ThresholdPreviewOut)
def threshold_preview(body: ThresholdPreviewIn) -> dict:
    """Derive a campaign's minimum quantity from its actual cost structure.

    This is the guardrail against the most expensive mistake in group buying:
    setting a threshold by intuition. Merchandisers run every campaign through
    this before it opens.
    """
    cost = CostModel(
        unit_cost_cents=body.unit_cost_cents,
        stop_cost_cents=body.stop_cost_cents,
        line_haul_cost_cents=body.line_haul_cost_cents,
        expected_units_per_run=body.expected_units_per_run,
        commission_bps=body.commission_bps,
    )
    analysis = min_viable_units(
        unit_price_cents=body.unit_price_cents,
        cost=cost,
        target_margin_cents=body.target_margin_cents,
        case_pack_units=body.case_pack_units,
        supplier_min_units=body.supplier_min_units,
    )

    tiers: list[TierOut] = []
    if analysis.viable and analysis.min_units:
        tiers = [
            TierOut(min_units=t.min_units, unit_price_cents=t.unit_price_cents)
            for t in suggest_tier_ladder(
                cost=cost,
                anchor_price_cents=body.unit_price_cents,
                threshold_units=analysis.min_units,
            )
        ]

    return {
        "viable": analysis.viable,
        "min_units": analysis.min_units,
        "raw_break_even_units": analysis.raw_break_even_units,
        "contribution_per_unit_cents": analysis.contribution_per_unit_cents,
        "fixed_cost_to_cover_cents": analysis.fixed_cost_to_cover_cents,
        "binding_constraint": analysis.binding_constraint,
        "reason": analysis.reason,
        "suggested_tiers": tiers,
    }
