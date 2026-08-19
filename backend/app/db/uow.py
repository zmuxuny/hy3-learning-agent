"""Central transaction boundary for short application database work.

Services and tool handlers stage changes with :func:`flush`; API/runtime
coordinators decide when the complete unit commits.  The helpers deliberately
do not retry an arbitrary pending ORM transaction: after SQLite reports a busy
writer, its business work may no longer be replay-safe.  Callers that own a
small, deterministic CAS operation can opt into :func:`run_short_transaction`,
which recreates the session and repeats only that bounded database callback.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


DEFAULT_RETRY_DELAYS: tuple[float, ...] = (0.025, 0.05, 0.1, 0.25)
_ResultT = TypeVar("_ResultT")


class DatabaseBusyError(RuntimeError):
    """Typed, retryable exhaustion of the SQLite writer budget."""

    code = "database_busy"
    error_code = "database_busy"
    retryable = True
    state = "database_busy"
    status = "database_busy"

    def __init__(
        self,
        message: str = "The database writer is busy; retry this durable action later.",
        *,
        retry_after_ms: int = 250,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.retry_after_ms = max(0, int(retry_after_ms))
        self.attempts = max(1, int(attempts))

    def as_result(self) -> dict[str, object]:
        """Return the stable wire shape used by runtime/API coordinators."""

        return {
            "ok": False,
            "error": str(self),
            "error_code": self.error_code,
            "code": self.code,
            "retryable": self.retryable,
            "state": self.state,
            "status": self.status,
            "retry_after_ms": self.retry_after_ms,
            "attempts": self.attempts,
        }


def is_database_busy(error: BaseException) -> bool:
    """Recognize only SQLite's writer-contention errors, not generic failures."""

    if not isinstance(error, OperationalError):
        return False
    original = getattr(error, "orig", error)
    sqlite_error_code = getattr(original, "sqlite_errorcode", None)
    if isinstance(sqlite_error_code, int) and (sqlite_error_code & 0xFF) in {5, 6}:
        # SQLite base codes: SQLITE_BUSY=5 and SQLITE_LOCKED=6.  Extended
        # result codes retain the base code in the low byte.
        return True
    message = str(original).lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "database is busy",
            "database table is busy",
        )
    )


async def _safe_rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        # Preserve the original database error.  The owning session context is
        # still responsible for closing the invalidated connection.
        pass


async def rollback(db: AsyncSession) -> None:
    """Roll back through the one application transaction coordinator."""

    await db.rollback()


async def ensure_sqlite_write_transaction(db: AsyncSession) -> None:
    """Physically begin SQLite's caller-owned write transaction when needed.

    Python's SQLite driver uses legacy transaction control: a SQLAlchemy
    transaction opened by a SELECT may not have emitted ``BEGIN`` to SQLite.
    In that state, releasing the first SAVEPOINT would commit it as the outer
    transaction.  Domain services that use a savepoint for unique-key race
    recovery call this helper before their first read, so the savepoint remains
    subordinate to the caller's Unit of Work and concurrent writers serialize
    before observing the idempotency key.
    """

    connection = await db.connection()
    if connection.dialect.name != "sqlite":
        return
    raw_connection = await connection.get_raw_connection()
    driver_connection = raw_connection.driver_connection
    if not bool(getattr(driver_connection, "in_transaction", False)):
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as exc:
            if not is_database_busy(exc):
                raise
            await _safe_rollback(db)
            raise DatabaseBusyError() from exc


async def flush(db: AsyncSession) -> None:
    """Flush staged rows and translate SQLite lock leakage into typed state."""

    try:
        await db.flush()
    except OperationalError as exc:
        if not is_database_busy(exc):
            raise
        await _safe_rollback(db)
        raise DatabaseBusyError() from exc


async def commit(db: AsyncSession) -> None:
    """Commit once; never replay the business work hidden in this session."""

    try:
        await db.commit()
    except OperationalError as exc:
        if not is_database_busy(exc):
            raise
        await _safe_rollback(db)
        raise DatabaseBusyError() from exc


@asynccontextmanager
async def unit_of_work(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Commit all staged work together, or release it together on failure."""

    try:
        yield db
        await commit(db)
    except BaseException:
        await _safe_rollback(db)
        raise


async def run_short_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    operation: Callable[[AsyncSession], Awaitable[_ResultT]],
    *,
    retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
) -> _ResultT:
    """Run one replay-safe DB-only callback with bounded writer backoff.

    This helper is for claims, compare-and-set transitions, event appends, and
    outbox receipt writes.  It must never wrap model, network, subprocess, or
    other external work because the callback can execute more than once.
    """

    delays = tuple(max(0.0, float(delay)) for delay in retry_delays)
    total_attempts = len(delays) + 1
    for index in range(total_attempts):
        try:
            async with session_factory() as db:
                try:
                    result = await operation(db)
                    await commit(db)
                    return result
                except OperationalError as exc:
                    if not is_database_busy(exc):
                        raise
                    await _safe_rollback(db)
                    raise DatabaseBusyError(attempts=index + 1) from exc
                except BaseException:
                    await _safe_rollback(db)
                    raise
        except DatabaseBusyError as exc:
            if index >= len(delays):
                retry_after_ms = int((delays[-1] if delays else 0.25) * 1000)
                raise DatabaseBusyError(
                    retry_after_ms=retry_after_ms,
                    attempts=total_attempts,
                ) from exc
            await asyncio.sleep(delays[index])

    raise AssertionError("short transaction retry loop exhausted unexpectedly")
