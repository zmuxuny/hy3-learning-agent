"""Canonical UTC handling for persisted and hashed timestamps.

New application writes and legacy database reads deliberately use separate
entry points.  New writes must carry an offset; legacy SQLite values without
one are interpreted as UTC because older releases discarded the offset and no
reliable local timezone can be reconstructed afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer

UTC = timezone.utc
_frozen_utc: ContextVar[datetime | None] = ContextVar(
    "learning_agent_frozen_utc",
    default=None,
)


def require_aware_utc(value: datetime) -> datetime:
    """Normalize a new timestamp to UTC and reject timezone-less input."""

    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("new persisted datetimes must include a UTC offset")
    return value.astimezone(UTC)


def coerce_legacy_utc(value: datetime) -> datetime:
    """Restore a historical SQLite value using the documented UTC policy."""

    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_legacy_datetime(value: datetime | str) -> datetime:
    """Parse a SQLite/ISO timestamp and apply the documented legacy policy."""

    if isinstance(value, datetime):
        return coerce_legacy_utc(value)
    if not isinstance(value, str):
        raise TypeError(f"expected datetime or ISO string, got {type(value).__name__}")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    return coerce_legacy_utc(datetime.fromisoformat(normalized))


def utc_now() -> datetime:
    """Return the current aware UTC instant."""

    frozen = _frozen_utc.get()
    return frozen if frozen is not None else datetime.now(UTC)


@contextmanager
def frozen_utc(value: datetime) -> Iterator[datetime]:
    """Scope application time to one aware instant without changing globals."""

    instant = require_aware_utc(value)
    token = _frozen_utc.set(instant)
    try:
        yield instant
    finally:
        _frozen_utc.reset(token)


def canonical_utc(value: datetime | None) -> str | None:
    """Serialize a persisted/legacy instant with fixed precision and ``Z``."""

    if value is None:
        return None
    return coerce_legacy_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


UTCInstant = Annotated[
    datetime,
    AfterValidator(require_aware_utc),
    PlainSerializer(canonical_utc, return_type=str, when_used="json"),
]
"""Pydantic instant type: aware input, normalized UTC, fixed JSON encoding."""
