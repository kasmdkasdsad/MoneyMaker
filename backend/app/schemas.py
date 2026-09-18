"""Pydantic request/response contracts.

Shaped for the mobile client. Two conventions worth noting:

* Money crosses the wire as integer cents under a ``_cents`` suffix. The client
  formats; the server never sends a pre-formatted currency string.
* Campaign responses carry the *derived social state* (``units_to_threshold``,
  ``units_to_next_tier``, ``is_secured``) rather than making the client compute
  it. That logic is the product, and it must not drift between iOS, Android and
  web.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TierOut(BaseModel):
    min_units: int
    unit_price_cents: int


class CampaignSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    image_url: str | None = None
    state: str
    unit_label: str

    current_unit_price_cents: int
    list_price_cents: int

    threshold_units: int
    committed_units: int
    available_units: int
    units_to_threshold: int
    progress_bps: int = Field(description="Progress toward threshold, 0-10000")
    is_secured: bool = Field(
        description="Threshold met: this campaign is going to ship"
    )

    units_to_next_tier: int | None = None
    next_tier_price_cents: int | None = None

    closes_at: datetime
    pickup_point_label: str
    pickup_distance_m: float | None = None


class CampaignDetailOut(CampaignSummaryOut):
    description: str | None = None
    tiers: list[TierOut] = []
    pickup_address_line: str
    delivery_eta: datetime | None = None
    case_pack_units: int


class JoinCampaignIn(BaseModel):
    quantity: int = Field(ge=1, le=1000)
    pickup_point_id: uuid.UUID | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    state: str
    quantity: int
    quoted_unit_price_cents: int
    charged_unit_price_cents: int | None = None
    total_quoted_cents: int
    reserved_until: datetime | None = None
    pickup_point_id: uuid.UUID


class ConfirmAuthorizationIn(BaseModel):
    psp_ref: str
    authorized_amount_cents: int = Field(ge=1)
    authorization_expires_at: datetime | None = None


class PickupPointOut(BaseModel):
    id: uuid.UUID
    label: str
    address_line: str
    distance_m: float
    remaining_capacity: int
    has_live_campaign: bool


class CloseDecisionOut(BaseModel):
    action: str
    reason: str
    committed_units: int
    threshold_units: int
    shortfall_units: int
    new_close_at: datetime | None = None


class ThresholdPreviewIn(BaseModel):
    """Ops tool: 'what threshold should this campaign have?'"""

    unit_price_cents: int = Field(ge=1)
    unit_cost_cents: int = Field(ge=1)
    stop_cost_cents: int = Field(ge=0)
    line_haul_cost_cents: int = Field(ge=0, default=0)
    expected_units_per_run: int = Field(ge=1, default=300)
    commission_bps: int = Field(ge=0, le=10_000, default=500)
    case_pack_units: int = Field(ge=1, default=1)
    supplier_min_units: int = Field(ge=0, default=0)
    target_margin_cents: int = Field(ge=0, default=0)


class ThresholdPreviewOut(BaseModel):
    viable: bool
    min_units: int | None
    raw_break_even_units: int | None
    contribution_per_unit_cents: int
    fixed_cost_to_cover_cents: int
    binding_constraint: str
    reason: str
    suggested_tiers: list[TierOut] = []


class ErrorOut(BaseModel):
    code: str
    message: str
