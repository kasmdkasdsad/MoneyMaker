"""Periodic sweeps.

Two jobs, both idempotent and both safe to run concurrently on several workers
(every mutation re-checks state under a row lock).

Cadence: every minute. Campaign deadlines are the moment buyers are watching
most closely -- a campaign that stays "closing in 0 minutes" for a quarter of an
hour reads as broken, and worse, it is the exact moment a near-miss campaign
needs its rescue extension to go out while people are still paying attention.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.domain.state import CampaignState
from app.domain.thresholds import CloseAction
from app.models import Campaign
from app.services.campaign_service import (
    close_campaign,
    sweep_expired_reservations,
    utcnow,
)

logger = logging.getLogger(__name__)


def find_due_campaigns(
    session: Session, *, now: datetime | None = None, limit: int = 200
) -> list[Campaign]:
    """Open campaigns whose window has run out, or that have hit their cap.

    Matches the ``campaigns_close_sweep_idx`` partial index on
    ``COALESCE(extended_until, closes_at)``.
    """
    now = now or utcnow()
    stmt = (
        select(Campaign)
        .where(
            Campaign.state == CampaignState.OPEN,
            or_(
                func.coalesce(Campaign.extended_until, Campaign.closes_at) <= now,
                Campaign.committed_units >= Campaign.cap_units,
            ),
        )
        .order_by(func.coalesce(Campaign.extended_until, Campaign.closes_at))
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def run_close_sweep(
    session: Session, *, now: datetime | None = None, limit: int = 200
) -> dict[str, int]:
    """Resolve every campaign whose window has closed.

    One campaign failing must not stall the rest of the sweep, so each is
    handled independently and errors are counted rather than raised.
    """
    now = now or utcnow()
    tally = {action.value: 0 for action in CloseAction}
    tally["error"] = 0

    for campaign in find_due_campaigns(session, now=now, limit=limit):
        try:
            decision = close_campaign(session, campaign_id=campaign.id, now=now)
            session.commit()
            tally[decision.action.value] += 1
            logger.info(
                "campaign %s -> %s (%s)",
                campaign.id,
                decision.action.value,
                decision.reason,
            )
        except Exception:
            session.rollback()
            tally["error"] += 1
            logger.exception("failed to close campaign %s", campaign.id)

    return tally


def run_reservation_sweep(
    session: Session, *, now: datetime | None = None, limit: int = 500
) -> int:
    """Release capacity held by checkouts that were abandoned mid-authorization."""
    now = now or utcnow()
    try:
        released = sweep_expired_reservations(session, now=now, limit=limit)
        session.commit()
        if released:
            logger.info("released %s reserved units", released)
        return released
    except Exception:
        session.rollback()
        logger.exception("reservation sweep failed")
        return 0


__all__ = ["find_due_campaigns", "run_close_sweep", "run_reservation_sweep"]
