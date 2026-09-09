from pathlib import Path

import app.api.workspace as workspace_api
import app.context.assembler as context_assembler
import app.core.config as config_module
import app.core.envfile as envfile_module
import app.tools.workspace as workspace_tools
from app.core.config import PROJECT_ROOT, settings

from conftest import TEST_DATABASE_DIR, TEST_DATABASE_PATH


def test_pytest_database_is_disposable_and_outside_repository_data():
    project_root = Path(PROJECT_ROOT).resolve()
    repository_data = (project_root / "data").resolve()
    database_path = TEST_DATABASE_PATH.resolve()

    assert TEST_DATABASE_DIR.name.startswith("learning-agent-pytest-")
    assert database_path.parent == TEST_DATABASE_DIR.resolve()
    assert repository_data not in database_path.parents
    assert project_root not in database_path.parents
    assert settings.DATABASE_URL == f"sqlite+aiosqlite:///{database_path}"


def test_external_test_integrations_are_fail_closed():
    assert settings.ENABLE_SCHEDULER is False
    assert settings.ENABLE_EMAIL_REPLY_POLLING is False
    assert settings.SMTP_HOST == ""
    assert settings.IMAP_HOST == ""
    assert settings.VAPID_PRIVATE_KEY == ""
    assert settings.OPENAI_API_BASE == "https://127.0.0.1:9/v1"


def test_runtime_file_writes_are_isolated_from_repository(
    isolated_runtime_root: Path,
):
    runtime_root = isolated_runtime_root.resolve()
    repository_root = Path(PROJECT_ROOT).resolve()
    workspace_root = (runtime_root / "data" / "workspace").resolve()

    assert repository_root not in runtime_root.parents
    assert config_module.PROJECT_ROOT.resolve() == runtime_root
    assert envfile_module.PROJECT_ROOT.resolve() == runtime_root
    assert context_assembler.PROJECT_ROOT.resolve() == runtime_root
    assert workspace_tools.WORKSPACE_ROOT.resolve() == workspace_root
    assert workspace_api.WORKSPACE_ROOT.resolve() == workspace_root
    assert workspace_api.UPLOAD_ROOT.resolve() == workspace_root / "uploads"
