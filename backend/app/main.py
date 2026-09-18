"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.domain.state import InvalidTransition
from app.errors import MoneyMakerError

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="MoneyMaker",
        version="0.1.0",
        description="Community group buying: threshold-locked campaigns with "
        "consolidated last-mile delivery.",
    )

    @app.exception_handler(MoneyMakerError)
    async def _domain_error(_: Request, exc: MoneyMakerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(InvalidTransition)
    async def _bad_transition(_: Request, exc: InvalidTransition) -> JSONResponse:
        # A rejected transition is a client-visible conflict, not a 500: it means
        # the campaign moved on while the buyer was looking at a stale screen.
        logger.warning("rejected transition: %s", exc)
        return JSONResponse(
            status_code=409,
            content={"code": "invalid_transition", "message": str(exc)},
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "environment": settings.environment}

    app.include_router(router)
    return app


app = create_app()
