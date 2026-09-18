"""SQLAlchemy ORM models.

A direct mapping of ``db/schema.sql``. The SQL file is the source of truth --
it carries the CHECK constraints and partial indexes that actually protect the
data, several of which have no ORM expression. These classes exist to give the
service layer typed access, not to generate the schema.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.state import CampaignState, OrderState


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


_campaign_state_enum = SAEnum(
    CampaignState, name="campaign_state", values_callable=lambda e: [m.value for m in e]
)
_order_state_enum = SAEnum(
    OrderState, name="order_state", values_callable=lambda e: [m.value for m in e]
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Identity & geography
# ---------------------------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    home_lat: Mapped[float | None] = mapped_column(Float)
    home_lon: Mapped[float | None] = mapped_column(Float)
    is_captain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Neighborhood(Base, TimestampMixin):
    __tablename__ = "neighborhoods"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    centroid_lat: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_lon: Mapped[float] = mapped_column(Float, nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, default=2500, nullable=False)
    delivery_dow: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    line_haul_cost_cents: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    stop_cost_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PickupPoint(Base, TimestampMixin):
    __tablename__ = "pickup_points"

    id: Mapped[uuid.UUID] = _uuid_pk()
    neighborhood_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("neighborhoods.id"), nullable=False
    )
    host_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(Text, nullable=False)
    address_line: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    capacity_units: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    commission_bps: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    pickup_window_hours: Mapped[int] = mapped_column(
        Integer, default=48, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    neighborhood: Mapped[Neighborhood] = relationship(lazy="joined")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class Supplier(Base, TimestampMixin):
    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(320))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    min_order_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_order_value_cents: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = _uuid_pk()
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False)


class ProductVariant(Base, TimestampMixin):
    __tablename__ = "product_variants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id"), nullable=False
    )
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    unit_label: Mapped[str] = mapped_column(Text, default="each", nullable=False)
    case_pack_units: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    volume_ml: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    product: Mapped[Product] = relationship(lazy="joined")


class SupplierOffer(Base):
    __tablename__ = "supplier_offers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id"), nullable=False
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_variants.id"), nullable=False
    )
    unit_cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_units: Mapped[int | None] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


class Campaign(Base, TimestampMixin):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = _uuid_pk()
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_variants.id"), nullable=False
    )
    supplier_offer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_offers.id"), nullable=False
    )
    neighborhood_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("neighborhoods.id"), nullable=False
    )
    pickup_point_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pickup_points.id"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    state: Mapped[CampaignState] = mapped_column(
        _campaign_state_enum, default=CampaignState.DRAFT, nullable=False
    )

    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    threshold_units: Mapped[int] = mapped_column(Integer, nullable=False)
    cap_units: Mapped[int] = mapped_column(Integer, nullable=False)
    case_pack_units: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    committed_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    unit_cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    list_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    locked_unit_price_cents: Mapped[int | None] = mapped_column(BigInteger)
    final_unit_price_cents: Mapped[int | None] = mapped_column(BigInteger)
    commission_bps: Mapped[int] = mapped_column(Integer, default=500, nullable=False)

    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    variant: Mapped[ProductVariant] = relationship(lazy="joined")
    pickup_point: Mapped[PickupPoint] = relationship(lazy="joined")
    tiers: Mapped[list["PriceTierRow"]] = relationship(
        back_populates="campaign", lazy="selectin", cascade="all, delete-orphan"
    )


class PriceTierRow(Base):
    __tablename__ = "price_tiers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    min_units: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    campaign: Mapped[Campaign] = relationship(back_populates="tiers")


class CampaignStop(Base):
    """Extra pickup points merged into a campaign to reach threshold."""

    __tablename__ = "campaign_stops"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    pickup_point_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pickup_points.id"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampaignEvent(Base):
    """Append-only. Never updated, never deleted."""

    __tablename__ = "campaign_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_state: Mapped[CampaignState | None] = mapped_column(_campaign_state_enum)
    to_state: Mapped[CampaignState | None] = mapped_column(_campaign_state_enum)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Orders & payments
# ---------------------------------------------------------------------------


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False
    )
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    pickup_point_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pickup_points.id"), nullable=False
    )
    state: Mapped[OrderState] = mapped_column(
        _order_state_enum, default=OrderState.PENDING_AUTH, nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    quoted_unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_unit_price_cents: Mapped[int | None] = mapped_column(BigInteger)
    reserved_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)


class PaymentIntent(Base, TimestampMixin):
    __tablename__ = "payment_intents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), nullable=False)
    psp: Mapped[str] = mapped_column(Text, default="stripe", nullable=False)
    psp_intent_ref: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        SAEnum(
            "authorizing", "authorized", "captured", "voided", "refunded", "failed",
            name="payment_state",
        ),
        default="authorizing",
        nullable=False,
    )
    authorized_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_amount_cents: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    refunded_amount_cents: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    authorization_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


# ---------------------------------------------------------------------------
# Fulfilment
# ---------------------------------------------------------------------------


class DeliveryRun(Base, TimestampMixin):
    __tablename__ = "delivery_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    neighborhood_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("neighborhoods.id"), nullable=False
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    carrier: Mapped[str | None] = mapped_column(Text)
    vehicle_capacity_units: Mapped[int] = mapped_column(
        Integer, default=600, nullable=False
    )
    state: Mapped[str] = mapped_column(
        SAEnum("planned", "dispatched", "completed", "failed", name="run_state"),
        default="planned",
        nullable=False,
    )
    line_haul_cost_cents: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    total_cost_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PurchaseOrder(Base, TimestampMixin):
    """What we actually buy from the supplier once a campaign locks.

    ``units`` is rounded up to a whole number of cases, so ``overage_units`` is
    stock we own and have not sold. Tracked explicitly because optimistic
    overage accounting is how a campaign that looks profitable turns out not to
    be.
    """

    __tablename__ = "purchase_orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False
    )
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    overage_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManifestItem(Base):
    """One buyer's parcel at one stop. The captain scans it out by pickup code."""

    __tablename__ = "manifest_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_stop_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_stops.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pickup_code: Mapped[str] = mapped_column(Text, nullable=False)


class Payout(Base):
    """Captain commission, paid per settled campaign."""

    __tablename__ = "payouts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    payee_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    psp_transfer_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunStop(Base):
    __tablename__ = "run_stops"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("delivery_runs.id", ondelete="CASCADE"), nullable=False
    )
    pickup_point_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pickup_points.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    planned_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_units: Mapped[int | None] = mapped_column(Integer)
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Money & reliability
# ---------------------------------------------------------------------------


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    txn_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    account: Mapped[str] = mapped_column(
        SAEnum(
            "buyer_escrow", "supplier_payable", "captain_payable", "delivery_cost",
            "platform_revenue", "refunds_payable",
            name="ledger_account",
        ),
        nullable=False,
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    memo: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxMessage(Base):
    """Transactional outbox: written with the state change, drained after it."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
