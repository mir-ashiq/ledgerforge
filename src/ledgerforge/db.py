"""Database engine/session management."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import get_settings

_engine = None
_session_factory: sessionmaker | None = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def session_factory() -> sessionmaker:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def reset_engine() -> None:
    """Dispose and forget the cached engine (used by tests and reconfig)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request. Engine functions manage
    their own commit/rollback; this only guarantees cleanup."""
    session = session_factory()()
    try:
        yield session
    finally:
        session.close()
