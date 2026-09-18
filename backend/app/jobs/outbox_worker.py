"""Transactional outbox drain.

Everything that touches the outside world -- payment authorization, capture,
void, supplier POs, push notifications -- happens here and nowhere else.

The delivery guarantee is at-least-once, so every handler must be idempotent.
For payments that idempotency is anchored on the PSP side: we pass the order id
as the provider's idempotency key, and a replayed capture returns the original
result instead of charging twice.

Failure handling is exponential backoff on ``available_at`` with a retry
ceiling. A message that exhausts its retries is left unprocessed with
``last_error`` populated -- deliberately, because the failures that reach that
point are money failures (an expired authorization, a declined capture) and they
need a human, not a silent drop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, OutboxMessage, PaymentIntent
from app.services.campaign_service import utcnow
from app.services.payments import PaymentGateway

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 5


def _backoff(attempts: int) -> timedelta:
    """Exponential backoff, capped at an hour."""
    return timedelta(seconds=min(3600, BASE_BACKOFF_SECONDS * (2 ** attempts)))


def claim_batch(
    session: Session, *, now: datetime | None = None, limit: int = 100
) -> list[OutboxMessage]:
    """Claim messages for this worker.

    ``FOR UPDATE SKIP LOCKED`` is what lets several workers drain the same
    outbox without coordinating: each claims a disjoint batch and never waits on
    another worker's rows.
    """
    now = now or utcnow()
    return list(
        session.execute(
            select(OutboxMessage)
            .where(
                OutboxMessage.processed_at.is_(None),
                OutboxMessage.available_at <= now,
                OutboxMessage.attempts < MAX_ATTEMPTS,
            )
            .order_by(OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars().all()
    )


def handle_message(
    session: Session, message: OutboxMessage, gateway: PaymentGateway, *, now: datetime
) -> None:
    """Dispatch one outbox message. Raising marks it for retry."""
    payload = message.payload or {}

    if message.event_type == "order.authorize_requested":
        order = session.get(Order, message.aggregate_id)
        if order is None:
            return
        result = gateway.authorize(
            order_id=str(order.id),
            amount_cents=int(payload["amount_cents"]),
            customer_ref=str(order.buyer_id),
            now=now,
        )
        # Import locally: the service layer imports nothing from jobs, and this
        # keeps that dependency arrow one-directional.
        from app.services.campaign_service import confirm_authorization

        confirm_authorization(
            session,
            order_id=order.id,
            psp_ref=result.psp_ref,
            authorized_amount_cents=result.authorized_amount_cents,
            authorization_expires_at=result.expires_at,
            now=now,
        )

    elif message.event_type == "order.capture_requested":
        intent = session.execute(
            select(PaymentIntent).where(
                PaymentIntent.order_id == message.aggregate_id
            )
        ).scalar_one_or_none()
        if intent is None or intent.psp_intent_ref is None:
            raise RuntimeError(f"no authorization for order {message.aggregate_id}")
        if intent.state == "captured":
            return  # already done; replayed message
        amount = int(payload["amount_cents"])
        gateway.capture(psp_ref=intent.psp_intent_ref, amount_cents=amount)
        intent.state = "captured"
        intent.captured_amount_cents = amount

    elif message.event_type == "order.void_requested":
        intent = session.execute(
            select(PaymentIntent).where(
                PaymentIntent.order_id == message.aggregate_id
            )
        ).scalar_one_or_none()
        if intent is None or intent.psp_intent_ref is None:
            return  # nothing was ever authorized
        if intent.state in ("voided", "refunded"):
            return
        gateway.void(psp_ref=intent.psp_intent_ref)
        intent.state = "voided"

    elif message.event_type in (
        "campaign.secured",
        "campaign.locked",
        "campaign.expired",
        "campaign.extended",
    ):
        # Notification fan-out. Wired to the push provider in Phase 1; logged
        # here so the events are observable from day one.
        logger.info(
            "notify %s for %s: %s",
            message.event_type,
            message.aggregate_id,
            payload,
        )

    else:
        logger.warning("unknown outbox event type: %s", message.event_type)


def drain_outbox(
    session: Session,
    gateway: PaymentGateway,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Process one batch. Returns a tally for metrics."""
    now = now or utcnow()
    tally = {"processed": 0, "failed": 0, "exhausted": 0}

    for message in claim_batch(session, now=now, limit=limit):
        try:
            handle_message(session, message, gateway, now=now)
            message.processed_at = now
            message.last_error = None
            session.commit()
            tally["processed"] += 1
        except Exception as exc:
            session.rollback()
            # Re-attach and record the failure in its own transaction so the
            # attempt count survives the rollback of the handler's work.
            fresh = session.get(OutboxMessage, message.id)
            if fresh is not None:
                fresh.attempts += 1
                fresh.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                fresh.available_at = now + _backoff(fresh.attempts)
                session.commit()
                if fresh.attempts >= MAX_ATTEMPTS:
                    tally["exhausted"] += 1
                    logger.error(
                        "outbox message %s exhausted retries: %s",
                        fresh.id,
                        fresh.last_error,
                    )
            tally["failed"] += 1
            logger.exception("outbox message %s failed", message.id)

    return tally


__all__ = ["MAX_ATTEMPTS", "claim_batch", "drain_outbox", "handle_message"]
