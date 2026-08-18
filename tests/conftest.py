import hashlib
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Every pytest process gets its own disposable SQLite file.  In particular,
# never point tests at data/learning_companion.db or a repository-local file
# that can be shared by another process after a crash.
TEST_DATABASE_TEMP_DIR = tempfile.TemporaryDirectory(prefix="learning-agent-pytest-")
TEST_DATABASE_DIR = Path(TEST_DATABASE_TEMP_DIR.name)
TEST_DATABASE_PATH = TEST_DATABASE_DIR / "learning_companion.db"
TEST_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory(prefix="learning-agent-bootstrap-")
TEST_BOOTSTRAP_ROOT = Path(TEST_BOOTSTRAP_TEMP_DIR.name)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DATABASE_PATH}"
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["OPENAI_API_BASE"] = "http://127.0.0.1:9/v1"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USERNAME"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["SMTP_FROM"] = ""
os.environ["SMTP_TO"] = ""
os.environ["IMAP_HOST"] = ""
os.environ["IMAP_USERNAME"] = ""
os.environ["IMAP_PASSWORD"] = ""
os.environ["ENABLE_EMAIL_REPLY_POLLING"] = "false"
os.environ["VAPID_PUBLIC_KEY"] = ""
os.environ["VAPID_PRIVATE_KEY"] = ""
os.environ["VAPID_SUBJECT"] = "mailto:test.invalid"

def _protected_runtime_fingerprint() -> str:
    """Hash runtime-owned paths without printing their names or contents."""

    digest = hashlib.sha256()
    targets = (
        PROJECT_ROOT / ".env",
        PROJECT_ROOT / ".env.tmp",
        PROJECT_ROOT / "..env.tmp",
        PROJECT_ROOT / "data" / "learning_companion.db",
        PROJECT_ROOT / "data" / "learning_companion.db-wal",
        PROJECT_ROOT / "data" / "learning_companion.db-shm",
        PROJECT_ROOT / "learning_companion.db",
        PROJECT_ROOT / "learning_companion.db-wal",
        PROJECT_ROOT / "learning_companion.db-shm",
        PROJECT_ROOT / "backend" / "learning_companion.db",
        PROJECT_ROOT / "backend" / "learning_companion.db-wal",
        PROJECT_ROOT / "backend" / "learning_companion.db-shm",
        PROJECT_ROOT / "backend" / "data" / "learning_companion.db",
        PROJECT_ROOT / "backend" / "data" / "learning_companion.db-wal",
        PROJECT_ROOT / "backend" / "data" / "learning_companion.db-shm",
        PROJECT_ROOT / "data" / "context",
        PROJECT_ROOT / "data" / "workspace",
        PROJECT_ROOT / "data" / "backups",
        PROJECT_ROOT / "data" / "demo-checkpoints",
    )
    for target in targets:
        paths = [target]
        if target.is_dir() and not target.is_symlink():
            paths.extend(sorted(target.rglob("*")))
        for path in paths:
            relative = path.relative_to(PROJECT_ROOT).as_posix().encode()
            digest.update(relative)
            if path.is_symlink():
                digest.update(b"symlink")
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                digest.update(b"file")
                digest.update(path.read_bytes())
            elif path.is_dir():
                digest.update(b"directory")
            else:
                digest.update(b"missing")
    return digest.hexdigest()


_PROTECTED_FINGERPRINT_BEFORE_IMPORT = _protected_runtime_fingerprint()
_REAL_PATH_MKDIR = Path.mkdir


def _redirect_import_time_runtime_mkdir(
    path: Path,
    mode: int = 0o777,
    parents: bool = False,
    exist_ok: bool = False,
) -> None:
    """Redirect config's eager runtime mkdir calls while app modules import."""

    try:
        relative = path.relative_to(PROJECT_ROOT / "data")
    except ValueError:
        target = path
    else:
        target = TEST_BOOTSTRAP_ROOT / "data" / relative
    _REAL_PATH_MKDIR(target, mode=mode, parents=parents, exist_ok=exist_ok)


# app.core.config eagerly creates runtime directories at module import.  Patch
# only that import window, then point modules that copy PROJECT_ROOT at import
# at the disposable bootstrap root.  Production files are not changed for H0.
Path.mkdir = _redirect_import_time_runtime_mkdir
try:
    import app.core.config as config_module  # noqa: E402

    config_module.PROJECT_ROOT = TEST_BOOTSTRAP_ROOT

    from app.db.database import AsyncSessionLocal, Base, engine  # noqa: E402
    from app.models import Owner, UserProfile  # noqa: E402
    import app.api.operations as operations_api  # noqa: E402
    import app.api.workspace as workspace_api  # noqa: E402
    import app.context.assembler as context_assembler  # noqa: E402
    import app.core.envfile as envfile_module  # noqa: E402
    import app.tools.workspace as workspace_tools  # noqa: E402
finally:
    Path.mkdir = _REAL_PATH_MKDIR

if _protected_runtime_fingerprint() != _PROTECTED_FINGERPRINT_BEFORE_IMPORT:
    raise RuntimeError("app import modified a protected runtime path")


@pytest.fixture(scope="session", autouse=True)
def protect_runtime_data_from_tests():
    """Fail the suite if any test mutates formal runtime data or local secrets."""

    before = _PROTECTED_FINGERPRINT_BEFORE_IMPORT
    if _protected_runtime_fingerprint() != before:
        raise RuntimeError("protected runtime path changed before pytest fixtures started")
    yield
    if _protected_runtime_fingerprint() != before:
        raise RuntimeError(
            "tests modified a protected runtime path; all writes must use pytest temp directories"
        )


@pytest.fixture(autouse=True)
def isolated_runtime_root(tmp_path: Path, monkeypatch) -> Path:
    """Keep Context, workspace, uploads, and env writes out of repository data."""

    runtime_root = tmp_path / "runtime-root"
    workspace_root = runtime_root / "data" / "workspace"
    upload_root = workspace_root / "uploads"
    (runtime_root / "data" / "context" / "plans").mkdir(parents=True)
    (runtime_root / "data" / "context" / "decisions").mkdir(parents=True)
    upload_root.mkdir(parents=True)

    monkeypatch.setattr(config_module, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(envfile_module, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(context_assembler, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(operations_api, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(workspace_api, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(workspace_api, "UPLOAD_ROOT", upload_root)
    return runtime_root


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        db.add(Owner(id="local", display_name="Test learner", timezone="Asia/Shanghai"))
        db.add(UserProfile(owner_id="local"))
        await db.commit()
    yield
