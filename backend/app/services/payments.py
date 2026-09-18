"""Payment gateway port.

The platform depends on this Protocol, never on a concrete PSP. Two reasons
beyond the usual testability argument:

* Group buying is authorize-now / capture-later, and PSPs differ sharply in how
  long an authorization survives and how partial capture behaves. Pinning that
  behind an interface keeps those differences from leaking into the threshold
  engine.
* Capture happens in bulk (one campaign lock can capture hundreds of intents).
  That fan-out belongs to the outbox worker, and the worker only needs these
  four verbs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from app.errors import PaymentFailed


@dataclass(frozen=True)
class AuthorizationResult:
    psp_ref: str
    authorized_amount_cents: int
    expires_at: datetime


class PaymentGateway(Protocol):
    def authorize(
        self, *, order_id: str, amount_cents: int, customer_ref: str, now: datetime
    ) -> AuthorizationResult:
        """Place a hold. Does not move money."""

    def capture(
        self, *, psp_ref: str, amount_cents: int
    ) -> None:
        """Capture up to the authorized amount. Partial capture is required:
        buyers who quoted a worse tier are charged the better final price."""

    def void(self, *, psp_ref: str) -> None:
        """Release a hold that was never captured -- the campaign failed."""

    def refund(self, *, psp_ref: str, amount_cents: int) -> None:
        """Return captured money after a post-lock failure."""


@dataclass
class FakePaymentGateway:
    """In-memory gateway for tests and local development.

    Models the one behaviour that actually bites in production: authorizations
    expire, and capturing an expired hold fails.
    """

    authorization_ttl_days: int = 7
    authorizations: dict[str, dict] = field(default_factory=dict)
    captured: dict[str, int] = field(default_factory=dict)
    voided: set[str] = field(default_factory=set)
    refunded: dict[str, int] = field(default_factory=dict)
    fail_next_authorize: bool = False

    def authorize(
        self, *, order_id: str, amount_cents: int, customer_ref: str, now: datetime
    ) -> AuthorizationResult:
        if self.fail_next_authorize:
            self.fail_next_authorize = False
            raise PaymentFailed("card declined")
        if amount_cents <= 0:
            raise PaymentFailed("authorization amount must be positive")
        ref = f"auth_{uuid.uuid4().hex[:16]}"
        expires_at = now + timedelta(days=self.authorization_ttl_days)
        self.authorizations[ref] = {
            "order_id": order_id,
            "amount_cents": amount_cents,
            "customer_ref": customer_ref,
            "expires_at": expires_at,
        }
        return AuthorizationResult(
            psp_ref=ref, authorized_amount_cents=amount_cents, expires_at=expires_at
        )

    def capture(self, *, psp_ref: str, amount_cents: int) -> None:
        auth = self.authorizations.get(psp_ref)
        if auth is None:
            raise PaymentFailed(f"unknown authorization {psp_ref}")
        if psp_ref in self.voided:
            raise PaymentFailed(f"authorization {psp_ref} was voided")
        if amount_cents > auth["amount_cents"]:
            raise PaymentFailed(
                f"cannot capture {amount_cents} against an authorization of "
                f"{auth['amount_cents']}"
            )
        self.captured[psp_ref] = self.captured.get(psp_ref, 0) + amount_cents

    def void(self, *, psp_ref: str) -> None:
        if psp_ref not in self.authorizations:
            raise PaymentFailed(f"unknown authorization {psp_ref}")
        self.voided.add(psp_ref)

    def refund(self, *, psp_ref: str, amount_cents: int) -> None:
        captured = self.captured.get(psp_ref, 0)
        already = self.refunded.get(psp_ref, 0)
        if already + amount_cents > captured:
            raise PaymentFailed("refund exceeds captured amount")
        self.refunded[psp_ref] = already + amount_cents


__all__ = [
    "AuthorizationResult",
    "FakePaymentGateway",
    "PaymentGateway",
]
