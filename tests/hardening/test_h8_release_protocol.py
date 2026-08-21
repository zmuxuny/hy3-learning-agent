from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILDER = PROJECT_ROOT / "scripts" / "build-release.py"
GATE = PROJECT_ROOT / "scripts" / "release-gate.py"
REQUIRED_GATES = (
    "lint_typecheck",
    "python_dependency_audit",
    "historical_migration",
    "evidence_audit",
    "context_isolation",
    "browser_matrix",
    "secret_scan",
    "frontend_build",
)


@pytest.fixture(autouse=True)
def clean_database():
    yield


def _release_source(path: Path) -> Path:
    path.mkdir(parents=True)
    for name in (".env.example", "CHANGELOG.md", "LICENSE", "README.md"):
        (path / name).write_text(f"public {name}\n", encoding="utf-8")
    files = {
        "backend/run.py": "print('runtime')\n",
        "backend/app/version.py": 'APPLICATION_VERSION = "9.8.7"\n',
        "scripts/start.sh": "#!/bin/sh\nexit 0\n",
        "scripts/setup.sh": "#!/bin/sh\nexit 0\n",
        "scripts/release-check.py": "raise SystemExit('must not ship')\n",
        "frontend/dist/index.html": "<!doctype html><title>fixture</title>\n",
        "frontend/dist/assets/app.js": "console.log('fixture')\n",
        "frontend/src/private.js": "should not ship\n",
        "data/private.db": "private database\n",
        ".env": "OPENAI_API_KEY=must-not-ship\n",
    }
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return path


def _build(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), "--source", str(source), "--output", str(output)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_release_archive_is_deterministic_manifested_and_contains_only_runtime_inputs(tmp_path: Path):
    source = _release_source(tmp_path / "source")
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"

    assert _build(source, first).returncode == 0
    assert _build(source, second).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.with_name("first.tar.sha256").read_text(encoding="ascii") == f"{digest}  first.tar\n"

    with tarfile.open(first) as archive:
        names = set(archive.getnames())
        prefix = "learning-agent-9.8.7"
        assert f"{prefix}/frontend/dist/index.html" in names
        assert f"{prefix}/scripts/start.sh" in names
        assert f"{prefix}/RELEASE-MANIFEST.json" in names
        assert all("/.env" not in name or name.endswith("/.env.example") for name in names)
        assert all("/data/" not in name for name in names)
        assert all("frontend/src" not in name and "node_modules" not in name for name in names)
        assert all("scripts/release-check.py" not in name for name in names)
        manifest = json.load(archive.extractfile(f"{prefix}/RELEASE-MANIFEST.json"))
        manifest_paths = {item["path"] for item in manifest["files"]}
        assert "frontend/dist/assets/app.js" in manifest_paths
        assert "frontend/src/private.js" not in manifest_paths
        assert "backend/requirements-dev.txt" not in manifest_paths
        assert "scripts/release-check.py" not in manifest_paths


def test_packaged_setup_uses_runtime_requirements_without_node(tmp_path: Path):
    source = _release_source(tmp_path / "source")
    (source / "scripts/setup.sh").write_bytes((PROJECT_ROOT / "scripts/setup.sh").read_bytes())
    (source / "backend/requirements.txt").write_text("fastapi>=0.115,<1\n", encoding="utf-8")
    (source / "backend/requirements-dev.txt").write_text(
        "-r requirements.txt\npytest>=9,<10\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / "release.tar"
    assert _build(source, archive_path).returncode == 0
    unpacked = tmp_path / "unpacked"
    with tarfile.open(archive_path) as archive:
        archive.extractall(unpacked, filter="data")
    release_root = next(unpacked.iterdir())

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    setup_log = tmp_path / "setup.log"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = '-m' ] && [ \"$2\" = 'venv' ]; then\n"
        "  mkdir -p .venv/bin\n"
        "  cp \"$0\" .venv/bin/python\n"
        "  cp \"$0\" .venv/bin/pip\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' \"$*\" >> \"$H8_SETUP_LOG\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_npm = fake_bin / "npm"
    npm_marker = tmp_path / "npm-invoked"
    fake_npm.write_text(
        "#!/bin/sh\n: > \"$H8_NPM_MARKER\"\nexit 99\n",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", "scripts/setup.sh"],
        cwd=release_root,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "H8_SETUP_LOG": str(setup_log),
            "H8_NPM_MARKER": str(npm_marker),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not npm_marker.exists()
    calls = setup_log.read_text(encoding="utf-8")
    assert "install -r backend/requirements.txt" in calls
    assert "requirements-dev.txt" not in calls


def test_release_builder_rejects_symlinked_runtime_input_and_missing_dist(tmp_path: Path):
    source = _release_source(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.write_text("do not package", encoding="utf-8")
    (source / "backend" / "escape.py").symlink_to(outside)

    symlinked = _build(source, tmp_path / "symlinked.tar")
    assert symlinked.returncode != 0
    assert "unsafe_release_symlink" in symlinked.stderr
    assert not (tmp_path / "symlinked.tar").exists()

    (source / "backend" / "escape.py").unlink()
    shutil.rmtree(source / "frontend" / "dist")
    missing = _build(source, tmp_path / "missing.tar")
    assert missing.returncode != 0
    assert "missing_release_asset" in missing.stderr


def test_release_builder_rejects_symlinked_source_and_output(tmp_path: Path):
    source = _release_source(tmp_path / "source")
    source_alias = tmp_path / "source-alias"
    source_alias.symlink_to(source, target_is_directory=True)
    aliased_source = _build(source_alias, tmp_path / "aliased-source.tar")
    assert aliased_source.returncode != 0
    assert "invalid_release_source" in aliased_source.stderr

    output_target = tmp_path / "existing-output.tar"
    output_target.write_text("do not overwrite\n", encoding="utf-8")
    output_alias = tmp_path / "output-alias.tar"
    output_alias.symlink_to(output_target)
    aliased_output = _build(source, output_alias)
    assert aliased_output.returncode != 0
    assert "unsafe_release_output" in aliased_output.stderr
    assert output_target.read_text(encoding="utf-8") == "do not overwrite\n"

    checksum_target = tmp_path / "existing-checksum.txt"
    checksum_target.write_text("do not overwrite\n", encoding="utf-8")
    checksum_output = tmp_path / "checksum-output.tar"
    checksum_output.with_name("checksum-output.tar.sha256").symlink_to(checksum_target)
    aliased_checksum = _build(source, checksum_output)
    assert aliased_checksum.returncode != 0
    assert "unsafe_release_checksum_output" in aliased_checksum.stderr
    assert not checksum_output.exists()
    assert checksum_target.read_text(encoding="utf-8") == "do not overwrite\n"


def _gate_repository(path: Path, *, failing: str | None = None) -> Path:
    (path / ".github/workflows").mkdir(parents=True)
    (path / "scripts").mkdir()
    commands = {}
    workflow = ["name: fixture", "on: [push]", "jobs:"]
    for gate in REQUIRED_GATES:
        marker = path / f"{gate}.txt"
        marker.write_text("fail\n" if gate == failing else "pass\n", encoding="utf-8")
        commands[gate] = [sys.executable, "scripts/check.py", gate]
        workflow.extend([f"  {gate}:", "    runs-on: ubuntu-latest", "    steps:", "      - run: true"])
    (path / "scripts/check.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "raise SystemExit(0 if Path(f'{sys.argv[1]}.txt').read_text().strip() == 'pass' else 9)\n",
        encoding="utf-8",
    )
    (path / "release-gates.json").write_text(
        json.dumps({"version": 1, "gates": commands}), encoding="utf-8",
    )
    (path / ".github/workflows/ci.yml").write_text("\n".join(workflow) + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture"],
        cwd=path,
        check=True,
    )
    return path


def _run_gate(repository: Path) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(GATE), "--repository", str(repository), "--format", "json"],
        cwd=PROJECT_ROOT,
        env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_release_gate_fails_closed_on_dirty_tree_and_on_first_failed_command(tmp_path: Path):
    dirty = _gate_repository(tmp_path / "dirty")
    (dirty / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    returncode, report = _run_gate(dirty)
    assert returncode != 0
    assert report["error"] == "release_repository_not_clean"
    assert report["failures"] == []

    failing = _gate_repository(tmp_path / "failing", failing="evidence_audit")
    returncode, report = _run_gate(failing)
    assert returncode != 0
    assert report["missing_gates"] == []
    assert report["failures"] == [{
        "gate": "evidence_audit",
        "returncode": 9,
        "stderr_tail": "",
    }]
