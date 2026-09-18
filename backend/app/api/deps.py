"""FastAPI dependencies."""

from __future__ import annotations

import uuid

from fastapi import Header, HTTPException, status

from app.config import Settings, get_settings


def settings_dep() -> Settings:
    return get_settings()


def current_user_id(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> uuid.UUID:
    """Placeholder authentication.

    PRODUCTION: replace with a verified JWT issued after SMS OTP. This product
    is phone-first -- there is no password to steal, and the phone number is
    also the pickup identity the captain checks against. The header form exists
    so the API is exercisable before auth lands, and it MUST NOT ship.
    """
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-User-Id"
        )
    try:
        return uuid.UUID(x_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="malformed X-User-Id"
        ) from exc


def idempotency_key(
    key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    """Required on every state-changing buyer request.

    Mobile clients retry. Without a client-supplied key, a retried join on a
    flaky connection becomes a second order against the buyer's card.
    """
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    if len(key) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key too long",
        )
    return key
