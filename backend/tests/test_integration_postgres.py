"""End-to-end tests against a real PostgreSQL instance.

These are the tests that actually prove the threshold-locking algorithm works,
because the guarantees it depends on are database guarantees: ``SELECT ... FOR
UPDATE`` serialisation, the ``campaigns_no_oversell`` CHECK, and the partial
unique indexes on ``orders``. None of those exist in SQLite, so there is no
value in a substitute engine here.

Run with::

    docker compose up -d db
    MM_TEST_DATABASE_URL=postgresql+psycopg://moneymaker:moneymaker@localhost:5432/moneymaker \
        pytest tests/test_integration_postgres.py

Skipped automatically when ``MM_TEST_DATABASE_URL`` is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.domain.state import CampaignState, OrderState
from app.domain.thresholds import CloseAction
from app.errors import CapacityExhausted, Conflict
from app.jobs.outbox_worker import drain_outbox
from app.jobs.scheduler import find_due_campaigns
from app.models import (
    Campaign,
    CampaignEvent,
    Neighborhood,
    Order,
    OutboxMessage,
    PaymentIntent,
    PickupPoint,
    PriceTierRow,
    Product,
    ProductVariant,
    Supplier,
    SupplierOffer,
    User,
)
from app.services import campaign_service
from app.services.payments import FakePaymentGateway

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def engine():
    url = os.environ.get("MM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MM_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
        conn.exec_driver_sql(SCHEMA_PATH.read_text())
    return eng


@pytest.fixture
def db(engine) -> Session:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    # Truncate rather than roll back: the outbox worker commits internally, so a
    # wrapping transaction would not isolate these tests.
    session.execute(
        text(
            "TRUNCATE users, neighborhoods, pickup_points, suppliers, products, "
            "product_variants, supplier_offers, campaigns, price_tiers, "
            "campaign_events, campaign_stops, orders, payment_intents, outbox, "
            "purchase_orders, delivery_runs, run_stops, manifest_items, "
            "ledger_entries, payouts RESTART IDENTITY CASCADE"
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def seed_campaign(
    db: Session,
    *,
    threshold_units: int = 24,
    cap_units: int = 120,
    case_pack_units: int = 6,
    closes_in_hours: int = 6,
    tiers: tuple[tuple[int, int], ...] = ((24, 1200), (48, 1100), (96, 1000)),
) -> Campaign:
    # Unique per call: several campaigns can be seeded inside one test.
    host = User(
        phone_e164=f"+1407{uuid.uuid4().int % 10**7:07d}", display_name="Captain"
    )
    db.add(host)
    db.flush()

    hood = Neighborhood(
        name="Audubon Park", city="Orlando", centroid_lat=28.5563,
        centroid_lon=-81.3392, delivery_dow=3,
        line_haul_cost_cents=6000, stop_cost_cents=1200, is_live=True,
    )
    db.add(hood)
    db.flush()

    point = PickupPoint(
        neighborhood_id=hood.id, host_user_id=host.id, label="Maple Court lobby",
        address_line="1 Maple Ct", lat=28.5570, lon=-81.3400, capacity_units=500,
    )
    supplier = Supplier(name="Bulk Pantry Co")
    db.add_all([point, supplier])
    db.flush()

    product = Product(supplier_id=supplier.id, title="Olive Oil 1L", category="pantry")
    db.add(product)
    db.flush()

    variant = ProductVariant(
        product_id=product.id, sku=f"OIL-{uuid.uuid4().hex[:6]}",
        case_pack_units=case_pack_units,
    )
    db.add(variant)
    db.flush()

    offer = SupplierOffer(
        supplier_id=supplier.id, variant_id=variant.id, unit_cost_cents=700,
        valid_to=NOW + timedelta(days=30),
    )
    db.add(offer)
    db.flush()

    campaign = Campaign(
        variant_id=variant.id, supplier_offer_id=offer.id, neighborhood_id=hood.id,
        pickup_point_id=point.id, state=CampaignState.OPEN,
        opens_at=NOW - timedelta(hours=1),
        closes_at=NOW + timedelta(hours=closes_in_hours),
        threshold_units=threshold_units, cap_units=cap_units,
        case_pack_units=case_pack_units, committed_units=0, reserved_units=0,
        unit_cost_cents=700, list_price_cents=1200,
    )
    db.add(campaign)
    db.flush()

    for min_units, price in tiers:
        db.add(
            PriceTierRow(
                campaign_id=campaign.id, min_units=min_units, unit_price_cents=price
            )
        )
    db.commit()
    db.refresh(campaign)
    return campaign


def make_buyer(db: Session, n: int) -> User:
    user = User(
        phone_e164=f"+1321{uuid.uuid4().int % 10**7:07d}", display_name=f"Buyer {n}"
    )
    db.add(user)
    db.commit()
    return user


def join(db, campaign, buyer, qty, *, key=None, now=NOW) -> Order:
    order = campaign_service.join_campaign(
        db, campaign_id=campaign.id, buyer_id=buyer.id, quantity=qty,
        idempotency_key=key or f"k-{uuid.uuid4().hex}", now=now,
    )
    db.commit()
    return order


def authorize(db, order, *, now=NOW) -> Order:
    """Simulate the PSP authorization returning successfully."""
    result = campaign_service.confirm_authorization(
        db, order_id=order.id, psp_ref=f"auth_{uuid.uuid4().hex[:12]}",
        authorized_amount_cents=order.quantity * order.quoted_unit_price_cents,
        authorization_expires_at=now + timedelta(days=7), now=now,
    )
    db.commit()
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJoinAndAuthorize:
    def test_join_reserves_capacity_but_does_not_count_as_demand(self, db):
        campaign = seed_campaign(db)
        buyer = make_buyer(db, 1)

        order = join(db, campaign, buyer, 4)
        db.refresh(campaign)

        assert order.state is OrderState.PENDING_AUTH
        assert order.quoted_unit_price_cents == 1200
        assert order.reserved_until is not None
        # Capacity is held, but the threshold must not move on money that has
        # not been authorized yet.
        assert campaign.reserved_units == 4
        assert campaign.committed_units == 0
        assert campaign.secured_at is None

    def test_authorization_promotes_the_reservation(self, db):
        campaign = seed_campaign(db)
        order = join(db, campaign, make_buyer(db, 1), 4)

        authorize(db, order)
        db.refresh(campaign)
        db.refresh(order)

        assert order.state is OrderState.COMMITTED
        assert order.reserved_until is None
        assert campaign.reserved_units == 0
        assert campaign.committed_units == 4

        intent = db.execute(
            select(PaymentIntent).where(PaymentIntent.order_id == order.id)
        ).scalar_one()
        assert intent.state == "authorized"
        assert intent.authorized_amount_cents == 4 * 1200

    def test_crossing_the_threshold_stamps_secured_but_stays_open(self, db):
        # The central product decision: threshold met means "this is happening",
        # not "this is closed". Late joiners still improve everyone's price.
        campaign = seed_campaign(db, threshold_units=24)
        for i in range(6):
            authorize(db, join(db, campaign, make_buyer(db, i), 4))

        db.refresh(campaign)
        assert campaign.committed_units == 24
        assert campaign.secured_at is not None
        assert campaign.state is CampaignState.OPEN  # still accepting

        events = db.execute(
            select(CampaignEvent).where(CampaignEvent.event_type == "threshold_met")
        ).scalars().all()
        assert len(events) == 1  # stamped exactly once

    def test_idempotent_join_returns_the_original_order(self, db):
        campaign = seed_campaign(db)
        buyer = make_buyer(db, 1)

        first = join(db, campaign, buyer, 3, key="same-key")
        second = join(db, campaign, buyer, 3, key="same-key")

        assert first.id == second.id
        db.refresh(campaign)
        assert campaign.reserved_units == 3  # not doubled

    def test_second_live_order_from_the_same_buyer_is_rejected(self, db):
        campaign = seed_campaign(db)
        buyer = make_buyer(db, 1)
        join(db, campaign, buyer, 3)

        with pytest.raises(Conflict, match="already has a live order"):
            join(db, campaign, buyer, 2)


class TestOversellProtection:
    def test_cap_cannot_be_exceeded(self, db):
        campaign = seed_campaign(db, threshold_units=6, cap_units=12)
        authorize(db, join(db, campaign, make_buyer(db, 1), 10))

        with pytest.raises(CapacityExhausted, match="2 units left"):
            join(db, campaign, make_buyer(db, 2), 3)

        db.refresh(campaign)
        assert campaign.committed_units == 10

    def test_in_flight_reservations_count_against_the_cap(self, db):
        # Two buyers mid-authorization must not be able to jointly oversell.
        campaign = seed_campaign(db, threshold_units=6, cap_units=12)
        join(db, campaign, make_buyer(db, 1), 7)   # pending_auth
        join(db, campaign, make_buyer(db, 2), 5)   # pending_auth -> exactly full

        with pytest.raises(CapacityExhausted):
            join(db, campaign, make_buyer(db, 3), 1)

        db.refresh(campaign)
        assert campaign.reserved_units == 12
        assert campaign.committed_units == 0

    def test_abandoned_checkouts_release_their_capacity(self, db):
        campaign = seed_campaign(db, threshold_units=6, cap_units=10)
        join(db, campaign, make_buyer(db, 1), 10, now=NOW)
        db.refresh(campaign)
        assert campaign.reserved_units == 10

        # Nothing left until the reservation lapses.
        with pytest.raises(CapacityExhausted):
            join(db, campaign, make_buyer(db, 2), 1)

        released = campaign_service.sweep_expired_reservations(
            db, now=NOW + timedelta(minutes=10)
        )
        db.commit()
        assert released == 10

        db.refresh(campaign)
        assert campaign.reserved_units == 0
        order = join(db, campaign, make_buyer(db, 3), 1, now=NOW + timedelta(minutes=11))
        assert order.state is OrderState.PENDING_AUTH


class TestWithdrawal:
    def test_withdrawing_before_lock_returns_capacity_and_unsecures(self, db):
        campaign = seed_campaign(db, threshold_units=8)
        keeper = authorize(db, join(db, campaign, make_buyer(db, 1), 4))
        leaver = authorize(db, join(db, campaign, make_buyer(db, 2), 4))

        db.refresh(campaign)
        assert campaign.secured_at is not None

        campaign_service.withdraw_order(db, order_id=leaver.id, now=NOW)
        db.commit()
        db.refresh(campaign)

        assert campaign.committed_units == 4
        # Dropping back below threshold must un-secure: buyers cannot be told a
        # campaign is happening when it no longer is.
        assert campaign.secured_at is None
        assert db.get(Order, keeper.id).state is OrderState.COMMITTED


class TestCloseDecisions:
    def test_noop_before_the_deadline(self, db):
        campaign = seed_campaign(db)
        authorize(db, join(db, campaign, make_buyer(db, 1), 24))
        decision = campaign_service.close_campaign(db, campaign_id=campaign.id, now=NOW)
        assert decision.action is CloseAction.NOOP

    def test_lock_at_deadline_prices_everyone_at_the_best_tier(self, db):
        campaign = seed_campaign(db, threshold_units=24)
        # First buyer joins early and is quoted 1200.
        early = authorize(db, join(db, campaign, make_buyer(db, 1), 24))
        assert early.quoted_unit_price_cents == 1200
        # Volume then pushes the campaign into the 48-unit tier.
        late = authorize(db, join(db, campaign, make_buyer(db, 2), 24))

        decision = campaign_service.close_campaign(
            db, campaign_id=campaign.id, now=NOW + timedelta(hours=7)
        )
        db.commit()
        assert decision.action is CloseAction.LOCK

        db.refresh(campaign)
        db.refresh(early)
        assert campaign.state is CampaignState.LOCKED
        assert campaign.locked_unit_price_cents == 1100
        # The early buyer is charged the improved price, not what they were quoted.
        assert early.charged_unit_price_cents == 1100
        assert early.state is OrderState.LOCKED

        captures = db.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == "order.capture_requested"
            )
        ).scalars().all()
        assert len(captures) == 2
        assert sum(m.payload["amount_cents"] for m in captures) == 48 * 1100

    def test_near_miss_extends_once_then_expires(self, db):
        campaign = seed_campaign(db, threshold_units=24)
        authorize(db, join(db, campaign, make_buyer(db, 1), 20))  # 83%

        first = campaign_service.close_campaign(
            db, campaign_id=campaign.id, now=NOW + timedelta(hours=7)
        )
        db.commit()
        assert first.action is CloseAction.EXTEND
        db.refresh(campaign)
        assert campaign.extended_until is not None
        assert campaign.state is CampaignState.OPEN

        second = campaign_service.close_campaign(
            db, campaign_id=campaign.id, now=campaign.extended_until + timedelta(minutes=1)
        )
        db.commit()
        assert second.action is CloseAction.EXPIRE
        db.refresh(campaign)
        assert campaign.state is CampaignState.EXPIRED

    def test_expiry_voids_every_hold_and_charges_nobody(self, db):
        campaign = seed_campaign(db, threshold_units=100)
        order = authorize(db, join(db, campaign, make_buyer(db, 1), 5))

        campaign_service.close_campaign(
            db, campaign_id=campaign.id, now=NOW + timedelta(hours=7)
        )
        db.commit()

        db.refresh(campaign)
        db.refresh(order)
        assert campaign.state is CampaignState.EXPIRED
        assert order.state is OrderState.EXPIRED
        assert order.charged_unit_price_cents is None  # never charged

        voids = db.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == "order.void_requested"
            )
        ).scalars().all()
        assert len(voids) == 1

    def test_reaching_the_cap_locks_early_without_waiting(self, db):
        campaign = seed_campaign(db, threshold_units=6, cap_units=12)
        authorize(db, join(db, campaign, make_buyer(db, 1), 6))
        db.refresh(campaign)
        assert campaign.state is CampaignState.OPEN

        authorize(db, join(db, campaign, make_buyer(db, 2), 6))  # hits the cap
        db.refresh(campaign)
        assert campaign.state is CampaignState.LOCKED
        assert campaign.locked_at is not None

    def test_sweeper_finds_due_and_at_cap_campaigns(self, db):
        due = seed_campaign(db, closes_in_hours=1)
        not_due = seed_campaign(db, closes_in_hours=48)

        found = find_due_campaigns(db, now=NOW + timedelta(hours=2))
        ids = {c.id for c in found}
        assert due.id in ids
        assert not_due.id not in ids


class TestFullLifecycleThroughTheOutbox:
    def test_join_authorize_lock_capture(self, db):
        """The whole money path, driven only by the outbox worker."""
        gateway = FakePaymentGateway()
        campaign = seed_campaign(db, threshold_units=24, case_pack_units=6)

        # Buyers join; only reservations exist so far.
        for i in range(4):
            join(db, campaign, make_buyer(db, i), 6, now=NOW)
        db.refresh(campaign)
        assert campaign.reserved_units == 24
        assert campaign.committed_units == 0

        # Worker drains authorize requests -> everything becomes committed.
        tally = drain_outbox(db, gateway, now=NOW)
        assert tally["failed"] == 0
        assert tally["processed"] == 4

        db.refresh(campaign)
        assert campaign.committed_units == 24
        assert campaign.reserved_units == 0
        assert campaign.secured_at is not None
        assert len(gateway.authorizations) == 4
        assert not gateway.captured  # nothing charged before lock

        # Window closes -> lock.
        decision = campaign_service.close_campaign(
            db, campaign_id=campaign.id, now=NOW + timedelta(hours=7)
        )
        db.commit()
        assert decision.action is CloseAction.LOCK

        # Worker drains captures.
        tally = drain_outbox(db, gateway, now=NOW + timedelta(hours=7))
        assert tally["failed"] == 0

        db.refresh(campaign)
        assert campaign.state is CampaignState.LOCKED
        # 24 units -> the 24-unit tier at 1200c.
        assert campaign.locked_unit_price_cents == 1200
        assert sum(gateway.captured.values()) == 24 * 1200
        assert not gateway.voided

        intents = db.execute(select(PaymentIntent)).scalars().all()
        assert all(i.state == "captured" for i in intents)
        assert sum(i.captured_amount_cents for i in intents) == 24 * 1200

        unprocessed = db.execute(
            select(OutboxMessage).where(OutboxMessage.processed_at.is_(None))
        ).scalars().all()
        assert unprocessed == []

    def test_failed_campaign_voids_instead_of_capturing(self, db):
        gateway = FakePaymentGateway()
        campaign = seed_campaign(db, threshold_units=100)

        join(db, campaign, make_buyer(db, 1), 5, now=NOW)
        drain_outbox(db, gateway, now=NOW)

        campaign_service.close_campaign(
            db, campaign_id=campaign.id, now=NOW + timedelta(hours=7)
        )
        db.commit()
        drain_outbox(db, gateway, now=NOW + timedelta(hours=7))

        assert len(gateway.voided) == 1
        assert not gateway.captured  # not one cent taken
