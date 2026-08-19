"""Dependency-free release and persistence version constants.

Maintenance commands import this module before settings or SQLAlchemy exist,
so it must remain standard-library-only and free of filesystem side effects.
Every schema change increments ``CURRENT_SCHEMA_VERSION`` and appends the
matching immutable revision in :mod:`app.db.migrations`.
"""

APPLICATION_VERSION = "1.1.1"
CURRENT_SCHEMA_VERSION = 2
