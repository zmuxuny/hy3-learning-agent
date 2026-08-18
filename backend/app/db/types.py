"""Database types shared by every persisted model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.core.time import parse_legacy_datetime, require_aware_utc


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC instants portably and always return aware UTC values.

    SQLite has no timezone-aware datetime storage.  It stores a naive UTC wall
    time, while this decorator restores ``timezone.utc`` on reads.  Databases
    with timezone support receive an aware UTC value directly.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        return dialect.type_descriptor(DateTime(timezone=dialect.name != "sqlite"))

    def process_bind_param(self, value: datetime | None, dialect: Dialect):
        if value is None:
            return None
        normalized = require_aware_utc(value)
        if dialect.name == "sqlite":
            return normalized.replace(tzinfo=None)
        return normalized

    def process_result_value(self, value: datetime | str | None, _dialect: Dialect):
        if value is None:
            return None
        return parse_legacy_datetime(value)
