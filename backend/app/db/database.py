import asyncio
from collections.abc import AsyncIterator
from contextlib import nullcontext
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.paths import lexical_absolute
from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {},
    echo=False,
)

if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # pragma: no cover - sqlite only
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def validate_runtime_database_target(state_root: Path) -> None:
    """Require the long-lived runtime lease to cover its SQLite database."""

    database_url = make_url(settings.DATABASE_URL)
    if database_url.get_backend_name() != "sqlite":
        return
    database_name = database_url.database
    if not database_name or database_name == ":memory:":
        return
    from app.db.maintenance import DATABASE_PATH_SET

    root = lexical_absolute(state_root)
    database_path = lexical_absolute(database_name)
    try:
        relative = database_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError(
            "Configured SQLite database is outside the leased runtime state root."
        ) from exc
    if relative not in DATABASE_PATH_SET:
        raise RuntimeError(
            "Configured SQLite database is not one of the managed runtime paths."
        )
    cursor = root
    for component in Path(relative).parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise RuntimeError("Configured SQLite database path contains a symbolic link.")


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


async def create_schema(
    *,
    state_lease_held: bool = False,
    state_root: Path | None = None,
) -> None:
    """Prepare the configured database using the canonical schema path."""

    import app.models  # noqa: F401 -- explicitly register every mapped model

    database_url = make_url(settings.DATABASE_URL)
    if database_url.get_backend_name() != "sqlite":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return

    database_name = database_url.database
    if not database_name or database_name == ":memory:":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return

    from app.db.migrations import migrate_sqlite_database
    from app.db.maintenance import runtime_state_lease
    from app.core.config import PROJECT_ROOT

    # Preserve the lexical final component so the migration layer can reject
    # a configured symlink instead of silently following it outside the state
    # root.  Parent-directory identity is validated by the runtime lease/CLI.
    database_path = lexical_absolute(database_name)
    effective_state_root = lexical_absolute(state_root or PROJECT_ROOT)
    try:
        database_identity = database_path.relative_to(effective_state_root).as_posix()
    except ValueError:
        database_identity = database_path.name
    await engine.dispose()

    def prepare_file_database() -> None:
        lifecycle = (
            nullcontext()
            if state_lease_held
            else runtime_state_lease(effective_state_root)
        )
        with lifecycle:
            migrate_sqlite_database(
                database_path,
                backup_root=database_path.parent / "backups" / "migrations",
                application_version=settings.VERSION,
                database_identity=database_identity,
            )

    await asyncio.to_thread(prepare_file_database)
