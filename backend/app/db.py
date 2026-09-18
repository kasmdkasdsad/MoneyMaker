"""Database engine and session plumbing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def build_engine():
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        future=True,
    )

    # A runaway query holding a campaign row lock would stall every buyer trying
    # to join that campaign, so every statement gets a hard ceiling.
    @event.listens_for(engine, "connect")
    def _set_statement_timeout(dbapi_connection, _record):  # pragma: no cover
        with dbapi_connection.cursor() as cursor:
            cursor.execute(
                f"SET statement_timeout = {settings.db_statement_timeout_ms}"
            )

    return engine


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_session_factory() -> sessionmaker[Session]:
    global _engine, _SessionLocal
    if _SessionLocal is None:
        _engine = build_engine()
        _SessionLocal = sessionmaker(
            bind=_engine, expire_on_commit=False, future=True
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
