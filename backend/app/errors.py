"""Domain-level errors, mapped to HTTP status codes in the API layer."""

from __future__ import annotations


class MoneyMakerError(Exception):
    """Base class. Carries a stable machine-readable code for clients."""

    code = "internal_error"
    status_code = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFound(MoneyMakerError):
    code = "not_found"
    status_code = 404


class Conflict(MoneyMakerError):
    """The request is valid but the current state forbids it."""

    code = "conflict"
    status_code = 409


class ValidationFailed(MoneyMakerError):
    code = "validation_failed"
    status_code = 422


class CapacityExhausted(Conflict):
    code = "insufficient_capacity"


class PaymentFailed(MoneyMakerError):
    code = "payment_failed"
    status_code = 402
