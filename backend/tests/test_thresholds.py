"""Threshold-locking engine tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.pricing import PriceTier
from app.domain.state import CampaignState, OrderState
from app.domain.thresholds import (
    CampaignSnapshot,
    CloseAction,
    LockPolicy,
    OrderSnapshot,
    RejectionCode,
    plan_close,
    plan_expiry,
    plan_lock,
    plan_reservation,
    should_lock_early,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

TIERS = (
    PriceTier(min_units=24, unit_price_cents=1200),
    PriceTier(min_units=48, unit_price_cents=1100),
    PriceTier(min_units=96, unit_price_cents=1000),
)


def snap(**overrides) -> CampaignSnapshot:
    base = dict(
        campaign_id="c1",
        state=CampaignState.OPEN,
        threshold_units=24,
        cap_units=120,
        case_pack_units=6,
        committed_units=0,
        reserved_units=0,
        opens_at=NOW - timedelta(days=1),
        closes_at=NOW + timedelta(hours=6),
        tiers=TIERS,
    )
    base.update(overrides)
    return CampaignSnapshot(**base)


def order(order_id: str, qty: int, quoted: int, state=OrderState.COMMITTED):
    return OrderSnapshot(
        order_id=order_id,
        buyer_id=f"b-{order_id}",
        quantity=qty,
        quoted_unit_price_cents=quoted,
        state=state,
    )


class TestSnapshotDerivations:
    def test_capacity_accounts_for_in_flight_reservations(self):
        s = snap(committed_units=30, reserved_units=10)
        assert s.held_units == 40
        assert s.available_units == 80

    def test_available_never_goes_negative(self):
        s = snap(committed_units=120, reserved_units=0, cap_units=120)
        assert s.available_units == 0

    def test_progress_is_capped_at_100_percent(self):
        assert snap(committed_units=12).progress_bps == 5000
        assert snap(committed_units=200, cap_units=250).progress_bps == 10_000

    def test_extension_moves_the_effective_deadline(self):
        extended = NOW + timedelta(hours=30)
        s = snap(extended_until=extended)
        assert s.effective_close_at == extended
        assert not s.is_due_for_close(NOW + timedelta(hours=7))

    def test_rejects_incoherent_snapshots(self):
        with pytest.raises(ValueError):
            snap(cap_units=10, threshold_units=24)
        with pytest.raises(ValueError):
            snap(closes_at=NOW - timedelta(days=2))


class TestReservation:
    def test_accepts_within_capacity_and_quotes_current_tier(self):
        plan = plan_reservation(snap(committed_units=30), requested_quantity=2, now=NOW)
        assert plan.accepted
        assert plan.units_delta == 2
        assert plan.quoted_unit_price_cents == 1200  # 30 units -> first tier
        assert plan.authorize_amount_cents == 2400
        assert plan.reserved_until == NOW + timedelta(seconds=180)

    def test_quote_is_a_ceiling_never_a_floor(self):
        # A buyer joining at 50 units quotes the 48-unit tier. Later volume can
        # only improve on it.
        plan = plan_reservation(snap(committed_units=50), requested_quantity=1, now=NOW)
        assert plan.quoted_unit_price_cents == 1100

    def test_rejects_when_capacity_is_exhausted(self):
        s = snap(committed_units=118, reserved_units=0, cap_units=120)
        plan = plan_reservation(s, requested_quantity=5, now=NOW)
        assert not plan.accepted
        assert plan.rejection is RejectionCode.INSUFFICIENT_CAPACITY
        assert plan.available_units == 2
        assert "2 units left" in plan.message

    def test_in_flight_reservations_prevent_overselling(self):
        # The whole reason reserved_units exists: 10 units are mid-authorization,
        # so only 10 of the nominal 20 remaining are actually claimable.
        s = snap(committed_units=100, reserved_units=10, cap_units=120)
        assert plan_reservation(s, requested_quantity=10, now=NOW).accepted
        assert not plan_reservation(s, requested_quantity=11, now=NOW).accepted

    def test_rejects_outside_the_window(self):
        closed = plan_reservation(
            snap(), requested_quantity=1, now=NOW + timedelta(hours=7)
        )
        assert closed.rejection is RejectionCode.WINDOW_CLOSED

        early = plan_reservation(
            snap(opens_at=NOW + timedelta(hours=1)), requested_quantity=1, now=NOW
        )
        assert early.rejection is RejectionCode.WINDOW_NOT_STARTED

    def test_rejects_when_campaign_is_not_open(self):
        plan = plan_reservation(
            snap(state=CampaignState.LOCKED, committed_units=24),
            requested_quantity=1,
            now=NOW,
        )
        assert plan.rejection is RejectionCode.NOT_OPEN

    def test_reducing_quantity_always_succeeds_and_returns_capacity(self):
        s = snap(committed_units=100, cap_units=120)
        plan = plan_reservation(
            s, requested_quantity=2, current_quantity=5, now=NOW
        )
        assert plan.accepted
        assert plan.units_delta == -3
        assert plan.available_units == s.available_units + 3

    def test_flags_the_commitment_that_crosses_the_threshold(self):
        s = snap(committed_units=22, threshold_units=24)
        assert plan_reservation(s, requested_quantity=2, now=NOW).would_secure
        assert not plan_reservation(s, requested_quantity=1, now=NOW).would_secure

    def test_already_secured_campaign_does_not_re_secure(self):
        s = snap(committed_units=30, threshold_units=24)
        assert not plan_reservation(s, requested_quantity=1, now=NOW).would_secure

    def test_rejects_nonsense_quantities(self):
        assert (
            plan_reservation(snap(), requested_quantity=-1, now=NOW).rejection
            is RejectionCode.INVALID_QUANTITY
        )
        assert (
            plan_reservation(
                snap(), requested_quantity=3, current_quantity=3, now=NOW
            ).rejection
            is RejectionCode.INVALID_QUANTITY
        )


class TestCloseDecision:
    def test_locks_when_threshold_is_met(self):
        d = plan_close(snap(committed_units=30), now=NOW + timedelta(hours=7))
        assert d.action is CloseAction.LOCK

    def test_does_nothing_before_the_deadline(self):
        d = plan_close(snap(committed_units=30), now=NOW)
        assert d.action is CloseAction.NOOP
        assert d.reason == "window still open"

    def test_extends_a_near_miss(self):
        # 20/24 = 83%, above the 80% rescue ratio.
        d = plan_close(snap(committed_units=20), now=NOW + timedelta(hours=7))
        assert d.action is CloseAction.EXTEND
        assert d.new_close_at == NOW + timedelta(hours=7) + timedelta(hours=24)
        assert d.shortfall_units == 4

    def test_expires_a_clear_miss(self):
        d = plan_close(snap(committed_units=5), now=NOW + timedelta(hours=7))
        assert d.action is CloseAction.EXPIRE
        assert d.shortfall_units == 19

    def test_expires_an_empty_campaign_rather_than_extending_it(self):
        d = plan_close(snap(committed_units=0), now=NOW + timedelta(hours=7))
        assert d.action is CloseAction.EXPIRE

    def test_extends_only_once(self):
        # A deadline that can always slip is not a deadline, and urgency is the
        # only reason anyone shares the link today.
        s = snap(committed_units=20, extended_until=NOW + timedelta(hours=30))
        d = plan_close(s, now=NOW + timedelta(hours=31))
        assert d.action is CloseAction.EXPIRE

    def test_rescue_can_be_disabled_by_policy(self):
        d = plan_close(
            snap(committed_units=20),
            now=NOW + timedelta(hours=7),
            policy=LockPolicy(allow_rescue=False),
        )
        assert d.action is CloseAction.EXPIRE

    def test_locks_early_when_the_cap_is_reached(self):
        # No upside left in waiting, so start fulfilment now.
        s = snap(committed_units=120, cap_units=120)
        assert should_lock_early(s)
        d = plan_close(s, now=NOW)
        assert d.action is CloseAction.LOCK
        assert "cap reached" in d.reason

    def test_does_not_lock_early_when_policy_forbids_it(self):
        s = snap(committed_units=120, cap_units=120)
        policy = LockPolicy(lock_early_at_cap=False)
        assert not should_lock_early(s, policy)
        assert plan_close(s, now=NOW, policy=policy).action is CloseAction.NOOP

    def test_non_open_campaign_is_a_noop(self):
        d = plan_close(
            snap(state=CampaignState.LOCKED, committed_units=24),
            now=NOW + timedelta(hours=7),
        )
        assert d.action is CloseAction.NOOP


class TestLockPlan:
    def test_refuses_to_lock_below_threshold(self):
        with pytest.raises(ValueError, match="cannot lock below threshold"):
            plan_lock(snap(committed_units=10), [], now=NOW)

    def test_charges_everyone_the_best_tier_reached(self):
        # Two early buyers quoted 1200; final volume hits the 48-unit tier, so
        # both are charged 1100 and neither is billed the difference.
        orders = [order("o1", 24, 1200), order("o2", 24, 1200)]
        plan = plan_lock(snap(committed_units=48), orders, now=NOW)
        assert plan.sold_units == 48
        assert plan.final_unit_price_cents == 1100
        assert plan.total_capture_cents == 48 * 1100
        assert plan.total_price_improvement_cents == 48 * 100
        for s in plan.settlements:
            assert s.final_unit_price_cents == 1100

    def test_never_charges_more_than_quoted(self):
        # A buyer holding a better quote than the campaign's final tier keeps it.
        # The campaign settles at the 24-unit tier (1200c), but this buyer was
        # quoted 1000c, so 1000c is what they are charged. The per-order quote is
        # a hard ceiling even when the campaign price is above it.
        orders = [order("o1", 24, 1000)]
        plan = plan_lock(snap(committed_units=24), orders, now=NOW)
        assert plan.final_unit_price_cents == 1200        # campaign tier price
        settlement = plan.settlements[0]
        assert settlement.final_unit_price_cents == 1000  # what this buyer pays
        assert settlement.capture_amount_cents == 24 * 1000
        assert settlement.price_improvement_cents == 0
        assert plan.total_capture_cents == 24 * 1000

    def test_rounds_purchase_up_to_whole_cases_and_reports_overage(self):
        orders = [order("o1", 26, 1200)]
        plan = plan_lock(
            snap(committed_units=26, case_pack_units=6), orders, now=NOW
        )
        assert plan.sold_units == 26
        assert plan.purchase_units == 30
        assert plan.overage_units == 4
        assert any("overage" in w for w in plan.warnings)

    def test_no_overage_when_volume_lands_on_a_case_boundary(self):
        orders = [order("o1", 24, 1200)]
        plan = plan_lock(
            snap(committed_units=24, case_pack_units=6), orders, now=NOW
        )
        assert plan.purchase_units == 24
        assert plan.overage_units == 0
        assert plan.warnings == ()

    def test_ignores_orders_that_do_not_count(self):
        orders = [
            order("o1", 24, 1200),
            order("o2", 50, 1200, state=OrderState.CANCELLED),
            order("o3", 50, 1200, state=OrderState.PENDING_AUTH),
        ]
        plan = plan_lock(snap(committed_units=24), orders, now=NOW)
        assert plan.sold_units == 24
        assert {s.order_id for s in plan.settlements} == {"o1"}

    def test_detects_counter_drift_and_prices_off_the_orders(self):
        # Campaign counter says 48, orders say 24. Orders win; drift is flagged.
        orders = [order("o1", 24, 1200)]
        plan = plan_lock(snap(committed_units=48), orders, now=NOW)
        assert plan.sold_units == 24
        assert plan.final_unit_price_cents == 1200
        assert any("counter drift" in w for w in plan.warnings)

    def test_flags_overage_that_would_breach_the_cap(self):
        s = snap(committed_units=119, cap_units=119, case_pack_units=6)
        plan = plan_lock(s, [order("o1", 119, 1000)], now=NOW)
        assert plan.purchase_units == 120
        assert plan.overage_exceeds_cap

    def test_is_deterministic(self):
        orders = [order("o2", 12, 1200), order("o1", 12, 1200)]
        a = plan_lock(snap(committed_units=24), orders, now=NOW)
        b = plan_lock(snap(committed_units=24), list(reversed(orders)), now=NOW)
        assert a == b


class TestExpiryPlan:
    def test_releases_every_uncaptured_hold(self):
        orders = [
            order("o1", 5, 1200),
            order("o2", 3, 1200, state=OrderState.PENDING_AUTH),
            order("o3", 9, 1200, state=OrderState.CANCELLED),
        ]
        plan = plan_expiry(snap(committed_units=8), orders)
        assert plan.void_order_ids == ("o1", "o2")
        assert plan.total_voided_cents == (5 + 3) * 1200
        assert plan.shortfall_units == 16


class TestConcurrencySimulation:
    def test_sequential_joins_never_oversell_the_cap(self):
        """Simulate a rush: every accepted reservation is applied, and capacity
        must hold at every step."""
        s = snap(committed_units=0, cap_units=50, threshold_units=24)
        accepted = 0
        for _ in range(100):
            plan = plan_reservation(s, requested_quantity=3, now=NOW)
            if not plan.accepted:
                continue
            s = s.with_reservation(plan.units_delta)
            s = s.with_confirmation(plan.units_delta, NOW)
            accepted += plan.units_delta
            assert s.held_units <= s.cap_units

        assert accepted == 48  # 16 x 3; a 17th would breach the 50 cap
        assert s.available_units == 2
        assert s.is_secured

    def test_secured_at_is_stamped_once_and_only_once(self):
        s = snap(committed_units=22, threshold_units=24)
        assert s.secured_at is None
        s = s.with_confirmation(2, NOW)
        assert s.secured_at == NOW
        later = NOW + timedelta(hours=1)
        s = s.with_confirmation(10, later)
        assert s.secured_at == NOW  # not re-stamped

    def test_withdrawals_return_capacity(self):
        s = snap(committed_units=30, cap_units=50)
        s = s.with_withdrawal(10)
        assert s.committed_units == 20
        assert s.available_units == 30
