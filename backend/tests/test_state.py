"""State machine tests."""

from __future__ import annotations

import pytest

from app.domain.state import (
    CAMPAIGN_TRANSITIONS,
    COUNTED_ORDER_STATES,
    ORDER_TRANSITIONS,
    TERMINAL_CAMPAIGN_STATES,
    TERMINAL_ORDER_STATES,
    CampaignState,
    InvalidTransition,
    OrderState,
    assert_campaign_transition,
    assert_order_transition,
    can_transition,
)


class TestCampaignTransitions:
    def test_happy_path_is_legal_end_to_end(self):
        path = [
            CampaignState.DRAFT, CampaignState.OPEN, CampaignState.LOCKED,
            CampaignState.PURCHASING, CampaignState.IN_TRANSIT,
            CampaignState.READY_FOR_PICKUP, CampaignState.SETTLED,
        ]
        for a, b in zip(path, path[1:]):
            assert_campaign_transition(a, b)

    def test_terminal_states_are_terminal(self):
        assert TERMINAL_CAMPAIGN_STATES == {
            CampaignState.SETTLED, CampaignState.EXPIRED, CampaignState.CANCELLED
        }
        for state in TERMINAL_CAMPAIGN_STATES:
            assert CAMPAIGN_TRANSITIONS[state] == frozenset()

    def test_expired_campaign_cannot_reopen(self):
        with pytest.raises(InvalidTransition, match="expired -> open"):
            assert_campaign_transition(CampaignState.EXPIRED, CampaignState.OPEN)

    def test_cannot_skip_straight_from_open_to_settled(self):
        assert not can_transition(CampaignState.OPEN, CampaignState.SETTLED)

    def test_cannot_cancel_once_stock_is_moving(self):
        # Past this point unwinding is an operational process, not a state flip.
        assert not can_transition(CampaignState.IN_TRANSIT, CampaignState.CANCELLED)
        assert can_transition(CampaignState.PURCHASING, CampaignState.CANCELLED)


class TestOrderTransitions:
    def test_happy_path_is_legal_end_to_end(self):
        path = [
            OrderState.PENDING_AUTH, OrderState.COMMITTED, OrderState.LOCKED,
            OrderState.READY_FOR_PICKUP, OrderState.PICKED_UP,
        ]
        for a, b in zip(path, path[1:]):
            assert_order_transition(a, b)

    def test_locked_order_cannot_be_cancelled_only_refunded(self):
        # Money is captured and the supplier PO is cut against this unit.
        assert not can_transition(OrderState.LOCKED, OrderState.CANCELLED)
        assert can_transition(OrderState.LOCKED, OrderState.REFUNDED)

    def test_terminal_order_states(self):
        assert TERMINAL_ORDER_STATES == {
            OrderState.PICKED_UP, OrderState.EXPIRED,
            OrderState.CANCELLED, OrderState.REFUNDED,
        }

    def test_only_real_commitments_count_toward_the_threshold(self):
        # A pending authorization holds capacity but must not be counted as
        # demand -- that is what would let a campaign lock on money that never
        # arrived.
        assert OrderState.PENDING_AUTH not in COUNTED_ORDER_STATES
        assert OrderState.CANCELLED not in COUNTED_ORDER_STATES
        assert OrderState.COMMITTED in COUNTED_ORDER_STATES
        assert OrderState.LOCKED in COUNTED_ORDER_STATES

    def test_every_state_appears_in_the_transition_table(self):
        assert set(ORDER_TRANSITIONS) == set(OrderState)
        assert set(CAMPAIGN_TRANSITIONS) == set(CampaignState)
