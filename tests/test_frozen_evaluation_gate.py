"""Current product coverage and immutable historical evaluation are both required."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clean_database():
    yield


@pytest.fixture
def gate():
    return _load_script("frozen-evaluation")


def _git(repository, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit_fixture(repository):
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "--quiet", "-m", "test fixture")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path, gate, monkeypatch):
    root = tmp_path / "repository"
    (root / "evaluation/tests").mkdir(parents=True)
    (root / "evaluation/artifact.json").write_text('{"frozen": true}\n')
    (root / "evaluation/tests/test_original.py").write_text("def test_original(): pass\n")
    (root / "backend").mkdir()
    (root / "backend/product.py").write_text("original runtime\n")
    (root / ".gitignore").write_text(".env\n")
    _git(root, "init", "--quiet")
    _git(root, "config", "core.filemode", "true")
    snapshot = _commit_fixture(root)
    monkeypatch.setattr(gate, "COMPETITION_SNAPSHOT", snapshot)
    # A newer product commit must never be substituted for the frozen runtime.
    (root / "backend/product.py").write_text("current runtime\n")
    _commit_fixture(root)
    return root


def test_competition_snapshot_is_pinned_to_full_commit(gate):
    assert gate.COMPETITION_SNAPSHOT == "7f31816b29bbb35390343db032af889d67efb0e7"


def test_historical_gate_keeps_both_stages_mandatory(monkeypatch):
    release = _load_script("release-check")
    calls = []
    monkeypatch.setattr(release, "_run", lambda command, **kwargs: calls.append(command))
    release.historical_migration()
    assert calls == [
        [sys.executable, "-m", "pytest", "-q", "tests"],
        [sys.executable, "scripts/frozen-evaluation.py"],
    ]
    manifest = json.loads((PROJECT_ROOT / "release-gates.json").read_text())
    assert len(manifest["gates"]) == 8
    assert set(manifest["gates"]) == set(release.GATES)
    assert "historical_migration" in manifest["gates"]
    assert "testpaths = tests\n" in (PROJECT_ROOT / "pytest.ini").read_text()
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text()
    historical_job = workflow.split("  historical_migration:\n", 1)[1].split("\n  evidence_audit:", 1)[0]
    assert "fetch-depth: 0" in historical_job


@pytest.mark.parametrize("failed_stage", [0, 1])
def test_either_stage_failure_fails_historical_gate(monkeypatch, failed_stage):
    release = _load_script("release-check")
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        if len(calls) == failed_stage + 1:
            raise RuntimeError("required stage failed")

    monkeypatch.setattr(release, "_run", fail)
    with pytest.raises(RuntimeError, match="required stage failed"):
        release.historical_migration()
    assert len(calls) == failed_stage + 1


@pytest.mark.parametrize("tamper", [
    "bytes", "staged_bytes", "deleted", "staged_deleted", "added", "untracked", "renamed",
    "mode", "staged_mode", "symlink", "directory_symlink", "directory",
])
def test_evaluation_tampering_is_rejected(gate, repository, tamper):
    artifact = repository / "evaluation/artifact.json"
    original = artifact.read_bytes()
    if tamper in {"bytes", "staged_bytes"}:
        artifact.write_text("tampered\n")
        if tamper == "staged_bytes":
            _git(repository, "add", str(artifact))
            artifact.write_bytes(original)
    elif tamper in {"deleted", "staged_deleted"}:
        artifact.unlink()
        if tamper == "staged_deleted":
            _git(repository, "add", "-u")
            artifact.write_bytes(original)
    elif tamper in {"added", "untracked"}:
        (repository / "evaluation/extra.json").write_text("extra\n")
        if tamper == "added":
            _git(repository, "add", "evaluation/extra.json")
    elif tamper == "renamed":
        _git(repository, "mv", "evaluation/artifact.json", "evaluation/renamed.json")
    elif tamper == "mode":
        artifact.chmod(0o755)
    elif tamper == "staged_mode":
        _git(repository, "update-index", "--chmod=+x", "evaluation/artifact.json")
    elif tamper == "symlink":
        target = repository / "same-bytes.json"
        target.write_bytes(original)
        artifact.unlink()
        artifact.symlink_to(target)
    elif tamper == "directory_symlink":
        directory = repository / "evaluation"
        target = repository / "evaluation-alias"
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    else:
        artifact.unlink()
        artifact.mkdir()
    before = _git(repository, "worktree", "list", "--porcelain")
    with pytest.raises((RuntimeError, OSError)):
        gate.run(repository)
    assert _git(repository, "worktree", "list", "--porcelain") == before


def test_unavailable_snapshot_fails_closed_without_fetch(gate, repository, monkeypatch):
    monkeypatch.setattr(gate, "COMPETITION_SNAPSHOT", "0" * 40)
    calls = []
    original_run = subprocess.run

    def capture(command, **kwargs):
        calls.append(command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(gate.subprocess, "run", capture)
    with pytest.raises(RuntimeError, match="frozen_evaluation_git_failed:cat-file"):
        gate.run(repository)
    assert len(calls) == 1
    assert "cat-file" in calls[0]


@pytest.mark.parametrize("outcome", ["pass", "fail", "timeout"])
def test_exact_snapshot_environment_and_cleanup(gate, repository, monkeypatch, outcome):
    # Synthetic local data must never be copied into the temporary worktree.
    (repository / ".env").write_text("fixture-only\n")
    for name in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "OPENAI_API_KEY",
                 "SMTP_PASSWORD", "IMAP_PASSWORD", "VAPID_PRIVATE_KEY", "GIT_DIR"):
        monkeypatch.setenv(name, "must-not-be-inherited")
    # _git here is the test's helper and should keep its normal Git context.
    monkeypatch.delenv("GIT_DIR")
    before = _git(repository, "worktree", "list", "--porcelain")
    original_run = subprocess.run
    worktrees = []

    def capture(command, **kwargs):
        if command[:3] != [sys.executable, "-m", "pytest"]:
            return original_run(command, **kwargs)
        checkout = kwargs["cwd"]
        worktrees.append(checkout)
        assert command == [sys.executable, "-m", "pytest", "-q", "evaluation/tests"]
        assert _git(checkout, "rev-parse", "HEAD") == gate.COMPETITION_SNAPSHOT
        assert _git(checkout, "status", "--porcelain") == ""
        assert (checkout / ".git").is_file()
        assert (checkout / "backend/product.py").read_text() == "original runtime\n"
        assert not (checkout / ".env").exists()
        assert "must-not-be-inherited" not in kwargs["env"].values()
        assert kwargs["env"]["OPENAI_API_BASE"] == "https://127.0.0.1:9/v1"
        assert kwargs["env"]["ENABLE_SCHEDULER"] == "false"
        assert kwargs["env"]["ENABLE_EMAIL_REPLY_POLLING"] == "false"
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 7200)
        return subprocess.CompletedProcess(command, 1 if outcome == "fail" else 0)

    monkeypatch.setattr(gate.subprocess, "run", capture)
    if outcome == "pass":
        gate.run(repository)
    else:
        with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
            gate.run(repository)
    assert len(worktrees) == 1
    assert not worktrees[0].exists()
    assert not worktrees[0].parent.exists()
    assert _git(repository, "worktree", "list", "--porcelain") == before
    assert (repository / "backend/product.py").read_text() == "current runtime\n"
