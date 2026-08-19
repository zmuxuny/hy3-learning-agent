from __future__ import annotations

import asyncio
import io
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import UploadFile

import app.api.settings as settings_api
import app.api.workspace as workspace_api
import app.core.envfile as envfile
from app.api.settings import NotificationPolicyUpdate
from app.core.config import settings
from app.core.envfile import update_env_file
from app.db.database import AsyncSessionLocal
from app.models import Notification, UserProfile
from app.notifications.service import NotificationService


async def _upload(filename: str, content: bytes) -> dict:
    upload = UploadFile(filename=filename, file=io.BytesIO(content))
    return await workspace_api.upload_workspace_file(upload)


@pytest.mark.asyncio
async def test_upload_has_stable_content_identity_under_retry_and_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    uploads = workspace / "uploads"
    monkeypatch.setattr(workspace_api, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(workspace_api, "UPLOAD_ROOT", uploads)
    content = b"print('complete artifact')\n"

    results = await asyncio.gather(
        *(
            _upload(
                "nested/solution.py" if index % 2 == 0 else "nested\\solution.py",
                content,
            )
            for index in range(12)
        )
    )

    paths = {result["path"] for result in results}
    assert len(paths) == 1
    assert {result["name"] for result in results} == {"solution.py"}
    target = workspace / paths.pop()
    assert target.read_bytes() == content
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [path for path in uploads.iterdir() if path.is_file()] == [target]
    assert not list(uploads.glob(".*.upload-tmp"))

    changed = await _upload("solution.py", b"print('different request')\n")
    assert changed["path"] != str(target.relative_to(workspace))
    assert (workspace / changed["path"]).read_bytes() == b"print('different request')\n"


@pytest.mark.asyncio
async def test_upload_replace_failure_never_exposes_half_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    uploads = workspace / "uploads"
    monkeypatch.setattr(workspace_api, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(workspace_api, "UPLOAD_ROOT", uploads)
    content = b"stable-complete-content\n"
    first = await _upload("artifact.txt", content)
    target = workspace / first["path"]

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected upload replace failure")

    monkeypatch.setattr(workspace_api.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected upload replace failure"):
        await _upload("artifact.txt", content)

    assert target.read_bytes() == content
    assert not list(uploads.glob(".*.upload-tmp"))


@pytest.mark.asyncio
async def test_upload_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    uploads = workspace / "uploads"
    monkeypatch.setattr(workspace_api, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(workspace_api, "UPLOAD_ROOT", uploads)
    original_fsync = os.fsync
    fsync_kinds: list[str] = []

    def observed_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(workspace_api.os, "fsync", observed_fsync)
    await _upload("durable.bin", b"durable bytes")

    assert fsync_kinds == ["file", "directory"]


def test_envfile_cross_process_updates_share_one_locked_rmw(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "BASE_FIXTURE=preserved\n"
        + "".join(f"PADDING_{index}=fixture\n" for index in range(5000)),
        encoding="utf-8",
    )
    process_count = 8
    start = tmp_path / "start"
    backend_root = Path(__file__).resolve().parents[2] / "backend"
    child_code = (
        "import sys,time; from pathlib import Path; "
        "from app.core.envfile import update_env_file; "
        "start=Path(sys.argv[1]); "
        "\nwhile not start.exists(): time.sleep(0.001)\n"
        "update_env_file({sys.argv[3]:sys.argv[4]}, Path(sys.argv[2]))"
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(start),
                str(env_path),
                f"CHILD_{index}",
                f"value-{index}",
            ],
            cwd=tmp_path,
            env={
                "PYTHONPATH": str(backend_root),
                "PYTHONNOUSERSITE": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(process_count)
    ]
    start.touch()
    results: list[tuple[int, str, str]] = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert results == [(0, "", "")] * process_count

    values = dict(
        line.split("=", 1)
        for line in env_path.read_text(encoding="utf-8").splitlines()
    )
    assert values["BASE_FIXTURE"] == "preserved"
    assert {values[f"CHILD_{index}"] for index in range(process_count)} == {
        f"value-{index}" for index in range(process_count)
    }
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.env-tmp"))


def test_envfile_replace_failure_preserves_original_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    original = b"SAFE_FIXTURE=before\n"
    env_path.write_bytes(original)

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected env replace failure")

    monkeypatch.setattr(envfile.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected env replace failure"):
        update_env_file({"SAFE_FIXTURE": "after"}, env_path)

    assert env_path.read_bytes() == original
    assert not list(tmp_path.glob(".*.env-tmp"))


def test_envfile_fsyncs_file_and_locked_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    original_fsync = os.fsync
    fsync_kinds: list[str] = []

    def observed_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(envfile.os, "fsync", observed_fsync)
    update_env_file({"SAFE_FIXTURE": "durable"}, env_path)

    assert fsync_kinds == ["file", "directory"]


@pytest.mark.asyncio
async def test_notification_policy_commit_failure_changes_neither_policy_nor_env(
    isolated_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = isolated_runtime_root / ".env"
    assert not env_path.exists()
    async with AsyncSessionLocal() as db:
        profile = await db.get(UserProfile, "local")
        profile.quiet_hours = {"start": "00:00", "end": "00:00"}
        profile.daily_notification_limit = 3
        profile.preferences = {"unrelated_fixture": "preserved"}
        await db.commit()

    async def fail_commit(_db: object) -> None:
        raise RuntimeError("injected notification policy commit failure")

    monkeypatch.setattr(settings_api, "commit_uow", fail_commit)
    monkeypatch.setattr(
        settings_api,
        "update_env_file",
        lambda _values: pytest.fail("notification policy must not write .env"),
    )
    async with AsyncSessionLocal() as db:
        with pytest.raises(RuntimeError, match="injected notification policy commit failure"):
            await settings_api.update_notification_policy(
                NotificationPolicyUpdate(
                    quiet_hours={"start": "22:00", "end": "07:00"},
                    daily_notification_limit=9,
                    cooldown_minutes=90,
                ),
                db,
            )

    async with AsyncSessionLocal() as db:
        profile = await db.get(UserProfile, "local")
        assert profile.quiet_hours == {"start": "00:00", "end": "00:00"}
        assert profile.daily_notification_limit == 3
        assert profile.preferences == {"unrelated_fixture": "preserved"}
    assert not env_path.exists()


@pytest.mark.asyncio
async def test_notification_guard_uses_profile_cooldown_and_preserves_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_NOTIFICATION_COOLDOWN_MINUTES", 60)
    async with AsyncSessionLocal() as db:
        profile = await db.get(UserProfile, "local")
        profile.quiet_hours = {"start": "00:00", "end": "00:00"}
        profile.daily_notification_limit = 10
        profile.preferences = {
            "unrelated_fixture": "preserved",
            "notification_cooldown_minutes": 10,
        }
        db.add(
            Notification(
                owner_id="local",
                channel="email",
                title="fixture",
                body="fixture",
                status="sent",
                sent_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        await db.commit()

        allowed, reason = await NotificationService(db)._guard(
            "local", "heartbeat", None
        )
        assert (allowed, reason) == (True, "allowed")

        result = await settings_api.update_notification_policy(
            NotificationPolicyUpdate(cooldown_minutes=90),
            db,
        )
        assert result["restart_required"] is False
        assert result["cooldown_minutes"] == 90
        assert profile.preferences == {
            "unrelated_fixture": "preserved",
            "notification_cooldown_minutes": 90,
        }
        rendered_settings = await settings_api.read_settings(db)
        assert rendered_settings["notification_cooldown_minutes"] == 90
        allowed, reason = await NotificationService(db)._guard(
            "local", "heartbeat", None
        )
        assert (allowed, reason) == (False, "notification cooldown")
