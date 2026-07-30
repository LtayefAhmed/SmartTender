"""Engine and session management.

Two engines, one schema:

* an **async** engine (asyncpg) for FastAPI request handlers, so the API never
  blocks its event loop on the database;
* a **sync** engine (psycopg3) for Celery workers, where the surrounding code
  is synchronous and an event loop per task would add cost without benefit.

Both are created lazily on first use. Creating them at import time would make
every worker and every test collection open connections it may never need, and
would fail hard in environments where PostgreSQL is intentionally absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_sync_engine: Engine | None = None
_sync_session_factory: sessionmaker[Session] | None = None


def _engine_kwargs() -> dict[str, Any]:
    settings = get_settings()
    return {
        "echo": settings.db.echo,
        "pool_size": settings.db.pool_size,
        "max_overflow": settings.db.max_overflow,
        "pool_recycle": settings.db.pool_recycle_seconds,
        "pool_pre_ping": settings.db.pool_pre_ping,
    }


def _install_statement_timeout(engine: Engine) -> None:
    """Have PostgreSQL cancel any statement that overruns the budget.

    Belt-and-braces alongside Celery's time limits: a lock wait inside the
    database is invisible to the worker's own timer, and an uncancelled one
    would hold a pool connection until the pool starves.
    """
    settings = get_settings()
    timeout_ms = settings.db.statement_timeout_ms
    if timeout_ms <= 0:
        return

    @event.listens_for(engine, "connect")
    def _set_timeout(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute(f"SET statement_timeout = {timeout_ms}")
            cursor.close()
        except Exception:
            logger.warning("statement_timeout.not_applied", timeout_ms=timeout_ms)


# ---------------------------------------------------------------------------
# Async (API)
# ---------------------------------------------------------------------------
def get_async_engine() -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        settings = get_settings()
        _async_engine = create_async_engine(settings.db.async_dsn, **_engine_kwargs())
        logger.info("db.async_engine.created", host=settings.db.host, database=settings.db.name)
    return _async_engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_async_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _async_session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Commits on success, rolls back on any exception."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for async code outside a request."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Sync (Celery workers)
# ---------------------------------------------------------------------------
def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        from sqlalchemy import create_engine

        settings = get_settings()
        _sync_engine = create_engine(settings.db.sync_dsn, **_engine_kwargs())
        _install_statement_timeout(_sync_engine)
        logger.info("db.sync_engine.created", host=settings.db.host, database=settings.db.name)
    return _sync_engine


def get_sync_session_factory() -> sessionmaker[Session]:
    global _sync_session_factory
    if _sync_session_factory is None:
        _sync_session_factory = sessionmaker(
            bind=get_sync_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sync_session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for worker code.

    Every Celery task body runs inside one of these. A task that raises leaves
    no partial write behind, which is what makes replay-after-crash safe.
    """
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Lifecycle & health
# ---------------------------------------------------------------------------
async def check_async_connection() -> bool:
    try:
        async with get_async_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("db.healthcheck.failed", error=str(exc))
        return False


def check_sync_connection() -> bool:
    try:
        with get_sync_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("db.healthcheck.failed", error=str(exc))
        return False


async def dispose_async_engine() -> None:
    global _async_engine, _async_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        _async_session_factory = None


def dispose_sync_engine() -> None:
    global _sync_engine, _sync_session_factory
    if _sync_engine is not None:
        _sync_engine.dispose()
        _sync_engine = None
        _sync_session_factory = None


def reset_engines() -> None:
    """Drop cached engines — used by tests that rebind the database URL."""
    global _async_engine, _async_session_factory, _sync_engine, _sync_session_factory
    _async_engine = None
    _async_session_factory = None
    _sync_engine = None
    _sync_session_factory = None


__all__ = [
    "async_session_scope",
    "check_async_connection",
    "check_sync_connection",
    "dispose_async_engine",
    "dispose_sync_engine",
    "get_async_engine",
    "get_async_session_factory",
    "get_db_session",
    "get_sync_engine",
    "get_sync_session_factory",
    "reset_engines",
    "session_scope",
]
