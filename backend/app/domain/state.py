"""Campaign and order state machines.

These enums mirror the PostgreSQL enums in ``db/schema.sql`` exactly. The legal
transitions live here rather than being scattered through service code, so
"can this campaign still be cancelled?" has exactly one answer in the codebase.

The database enforces the *shape* of a state (a locked campaign must have a
locked price -- see the ``campaigns_locked_consistent`` CHECK). This module
enforces the *path* between states. Both matter: the CHECK stops a bad row, the
transition table stops a bad workflow.
"""

from __future__ import annotations

from enum import StrEnum


class CampaignState(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    LOCKED = "locked"
    PURCHASING = "purchasing"
    IN_TRANSIT = "in_transit"
    READY_FOR_PICKUP = "ready_for_pickup"
    SETTLED = "settled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OrderState(StrEnum):
    PENDING_AUTH = "pending_auth"
    COMMITTED = "committed"
    LOCKED = "locked"
    READY_FOR_PICKUP = "ready_for_pickup"
    PICKED_UP = "picked_up"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class InvalidTransition(Exception):
    """Raised when code attempts a transition the state machine forbids."""

    def __init__(self, entity: str, from_state: str, to_state: str) -> None:
        super().__init__(
            f"illegal {entity} transition: {from_state} -> {to_state}"
        )
        self.entity = entity
        self.from_state = from_state
        self.to_state = to_state


# A campaign can be cancelled by ops from any pre-fulfilment state; once stock
# is physically moving, unwinding is an operational process, not a state flip.
CAMPAIGN_TRANSITIONS: dict[CampaignState, frozenset[CampaignState]] = {
    CampaignState.DRAFT: frozenset({CampaignState.OPEN, CampaignState.CANCELLED}),
    CampaignState.OPEN: frozenset(
        {CampaignState.LOCKED, CampaignState.EXPIRED, CampaignState.CANCELLED}
    ),
    CampaignState.LOCKED: frozenset(
        {CampaignState.PURCHASING, CampaignState.CANCELLED}
    ),
    CampaignState.PURCHASING: frozenset(
        {CampaignState.IN_TRANSIT, CampaignState.CANCELLED}
    ),
    CampaignState.IN_TRANSIT: frozenset({CampaignState.READY_FOR_PICKUP}),
    CampaignState.READY_FOR_PICKUP: frozenset({CampaignState.SETTLED}),
    CampaignState.SETTLED: frozenset(),
    CampaignState.EXPIRED: frozenset(),
    CampaignState.CANCELLED: frozenset(),
}

ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    # Authorization either lands (committed) or does not (cancelled/expired).
    OrderState.PENDING_AUTH: frozenset(
        {OrderState.COMMITTED, OrderState.CANCELLED, OrderState.EXPIRED}
    ),
    # While merely committed the buyer is free to walk: no money has moved.
    OrderState.COMMITTED: frozenset(
        {OrderState.LOCKED, OrderState.CANCELLED, OrderState.EXPIRED}
    ),
    # After lock the money is captured and the supplier PO is real. The only
    # ways out are fulfilment or a refund.
    OrderState.LOCKED: frozenset(
        {OrderState.READY_FOR_PICKUP, OrderState.REFUNDED}
    ),
    OrderState.READY_FOR_PICKUP: frozenset(
        {OrderState.PICKED_UP, OrderState.REFUNDED}
    ),
    OrderState.PICKED_UP: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REFUNDED: frozenset(),
}

TERMINAL_CAMPAIGN_STATES = frozenset(
    s for s, nxt in CAMPAIGN_TRANSITIONS.items() if not nxt
)
TERMINAL_ORDER_STATES = frozenset(
    s for s, nxt in ORDER_TRANSITIONS.items() if not nxt
)

# Orders in these states hold capacity and count toward the threshold.
COUNTED_ORDER_STATES = frozenset(
    {
        OrderState.COMMITTED,
        OrderState.LOCKED,
        OrderState.READY_FOR_PICKUP,
        OrderState.PICKED_UP,
    }
)

# States in which a buyer may still change their mind for free.
WITHDRAWABLE_ORDER_STATES = frozenset(
    {OrderState.PENDING_AUTH, OrderState.COMMITTED}
)


def can_transition(
    from_state: CampaignState | OrderState, to_state: CampaignState | OrderState
) -> bool:
    table = (
        CAMPAIGN_TRANSITIONS
        if isinstance(from_state, CampaignState)
        else ORDER_TRANSITIONS
    )
    return to_state in table.get(from_state, frozenset())


def assert_campaign_transition(
    from_state: CampaignState, to_state: CampaignState
) -> None:
    if not can_transition(from_state, to_state):
        raise InvalidTransition("campaign", from_state, to_state)


def assert_order_transition(from_state: OrderState, to_state: OrderState) -> None:
    if not can_transition(from_state, to_state):
        raise InvalidTransition("order", from_state, to_state)


__all__ = [
    "CAMPAIGN_TRANSITIONS",
    "COUNTED_ORDER_STATES",
    "CampaignState",
    "InvalidTransition",
    "ORDER_TRANSITIONS",
    "OrderState",
    "TERMINAL_CAMPAIGN_STATES",
    "TERMINAL_ORDER_STATES",
    "WITHDRAWABLE_ORDER_STATES",
    "assert_campaign_transition",
    "assert_order_transition",
    "can_transition",
]
