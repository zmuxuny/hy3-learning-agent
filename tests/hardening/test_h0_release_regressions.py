from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROUTER = PROJECT_ROOT / "frontend" / "src" / "router.js"
REQUIRED_CI_GATES = (
    "lint_typecheck",
    "python_dependency_audit",
    "historical_migration",
    "evidence_audit",
    "context_isolation",
    "browser_matrix",
    "secret_scan",
    "frontend_build",
)
TRACKED_RELEASE_FILES = {".env.example", "CHANGELOG.md", "LICENSE", "README.md"}
TRACKED_RELEASE_DIRECTORIES = {"assets", "backend", "docs", "frontend", "scripts"}


class HarnessError(RuntimeError):
    """The regression harness itself is unavailable or malformed."""


def _require_harness(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessError(message)


@pytest.fixture(autouse=True)
def clean_database():
    """Shadow the app-wide database reset; these tests use only synthetic repositories."""
    yield


def _synthetic_repository(tmp_path: Path, *script_names: str) -> tuple[Path, dict[str, str]]:
    """Copy maintenance scripts into an isolated repository-shaped fixture."""
    try:
        repository = tmp_path / "synthetic-repository"
        scripts_dir = repository / "scripts"
        data_dir = repository / "data"
        fake_bin = repository / "fake-bin"
        scripts_dir.mkdir(parents=True)
        data_dir.mkdir()
        fake_bin.mkdir()

        for script_name in script_names:
            source = PROJECT_ROOT / "scripts" / script_name
            if not source.is_file():
                raise HarnessError(f"required maintenance fixture is missing: {script_name}")
            shutil.copy2(source, scripts_dir / script_name)

        if "start.sh" in script_names:
            # Release probes must not depend on a service that happens to be
            # listening on the host's port 8000.
            fake_curl = fake_bin / "curl"
            fake_curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
            fake_curl.chmod(0o755)

        if set(script_names).intersection(
            {"reset-data.sh", "seed-fixture.sh", "demo-data.sh"}
        ):
            driver_source = PROJECT_ROOT / "scripts" / "data-maintenance.py"
            core_source = PROJECT_ROOT / "backend" / "app" / "db" / "maintenance.py"
            version_source = PROJECT_ROOT / "backend" / "app" / "version.py"
            if (
                not driver_source.is_file()
                or not core_source.is_file()
                or not version_source.is_file()
            ):
                raise HarnessError("safe data-maintenance implementation is missing")
            shutil.copy2(driver_source, scripts_dir / "data-maintenance.py")
            core_destination = repository / "backend" / "app" / "db" / "maintenance.py"
            core_destination.parent.mkdir(parents=True)
            shutil.copy2(core_source, core_destination)
            shutil.copy2(version_source, repository / "backend" / "app" / "version.py")

    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError("could not construct the synthetic repository") from exc

    environment = {
        "PATH": f"{fake_bin}{os.pathsep}{os.defpath}",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    return repository, environment


def _run_script(
    repository: Path,
    environment: dict[str, str],
    name: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return _run_harness_command(
        [str(repository / "scripts" / name), *arguments],
        cwd=repository,
        env=environment,
        timeout=10,
    )


def _run_harness_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    text: bool = True,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=text,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(f"harness command timed out: {command[0]}") from exc
    except OSError as exc:
        raise HarnessError(f"harness command could not start: {command[0]}") from exc


def _maintenance_error_code(result: subprocess.CompletedProcess[str]) -> str | None:
    try:
        report = json.loads(result.stderr)
    except json.JSONDecodeError:
        return None
    return report.get("code") if isinstance(report, dict) else None


def _published_backup(repository: Path, purpose: str) -> Path:
    candidates = sorted(
        path
        for path in (repository / "data" / "backups").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    matching = []
    for candidate in candidates:
        try:
            manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("maintenance fixture emitted an unreadable manifest") from exc
        if manifest.get("purpose") == purpose:
            matching.append(candidate)
    if len(matching) != 1:
        raise HarnessError(f"expected one published {purpose} backup, got {len(matching)}")
    return matching[0]


def _create_database(path: Path, marker: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE fixture_marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO fixture_marker (value) VALUES (?)", (marker,))
    except (OSError, sqlite3.Error) as exc:
        raise HarnessError(f"could not create SQLite fixture at {path.name}") from exc


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise HarnessError(f"could not hash fixture path: {path.name}") from exc


def _sqlite_runtime_snapshot(database_path: Path) -> dict[str, dict | None]:
    snapshot: dict[str, dict | None] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database_path.with_name(database_path.name + suffix)
        try:
            if not path.exists():
                snapshot[suffix or "database"] = None
                continue
            stat_result = path.stat()
            snapshot[suffix or "database"] = {
                "inode": stat_result.st_ino,
                "size": stat_result.st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        except OSError as exc:
            raise HarnessError("could not snapshot the synthetic SQLite files") from exc
    return snapshot


def _database_integrity(path: Path) -> list[tuple[str]] | None:
    try:
        with sqlite3.connect(path) as connection:
            return connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError:
        return None
    except OSError as exc:
        raise HarnessError(f"could not inspect SQLite fixture: {path.name}") from exc


def _fixture_marker(path: Path) -> tuple[str] | None:
    try:
        with sqlite3.connect(path) as connection:
            return connection.execute("SELECT value FROM fixture_marker").fetchone()
    except sqlite3.DatabaseError:
        return None
    except OSError as exc:
        raise HarnessError(f"could not read SQLite fixture: {path.name}") from exc


def _write_corrupt_database_fixture(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not-a-sqlite-database")
    except OSError as exc:
        raise HarnessError("could not create the corrupt SQLite fixture") from exc


@contextmanager
def _held_write_transaction(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HarnessError("could not prepare the synthetic SQLite writer") from exc
    program = """
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("CREATE TABLE fixture_marker (value TEXT NOT NULL)")
connection.commit()
connection.execute("BEGIN IMMEDIATE")
connection.execute("INSERT INTO fixture_marker (value) VALUES ('uncommitted')")
print("ready", flush=True)
sys.stdin.readline()
connection.rollback()
connection.close()
"""
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", program, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = process.stdout.readline() if process.stdout is not None else ""
    except OSError as exc:
        raise HarnessError("could not hold the synthetic SQLite writer") from exc
    if ready != "ready\n":
        stderr = process.stderr.read() if process.stderr is not None else ""
        process.kill()
        process.wait()
        raise HarnessError(f"synthetic SQLite writer did not start: {stderr[-1000:]}")
    try:
        yield
    finally:
        try:
            if process.stdin is not None:
                process.stdin.write("\n")
                process.stdin.flush()
            returncode = process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            process.kill()
            process.wait()
            raise HarnessError("could not close the synthetic SQLite writer") from exc
        if returncode != 0:
            raise HarnessError("synthetic SQLite writer exited unsuccessfully")


def _run_node_contract(source: str) -> dict:
    node = shutil.which("node")
    if node is None:
        raise HarnessError("Node is required for the frontend state harness")
    result = _run_harness_command(
        [node, "--input-type=module", "--eval", source],
        cwd=PROJECT_ROOT,
        env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        timeout=15,
    )
    if result.returncode != 0:
        raise HarnessError(
            f"Node state harness exited with {result.returncode}: {result.stderr[-1000:]}"
        )
    try:
        report = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HarnessError("Node state harness did not return one JSON report") from exc
    if not isinstance(report, dict):
        raise HarnessError("Node state harness JSON report must be an object")
    return report


def _materialize_worktree_release(target: Path) -> dict:
    git = shutil.which("git")
    if git is None:
        raise HarnessError("git is required to construct the worktree release fixture")
    listed = _run_harness_command(
        [git, "ls-files", "-z", "--cached"],
        cwd=PROJECT_ROOT,
        env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        timeout=15,
        text=False,
    )
    if listed.returncode != 0:
        raise HarnessError(
            "git could not enumerate tracked release inputs: "
            + listed.stderr.decode("utf-8", errors="replace")[-1000:]
        )

    source_copy = target / "release-source"
    try:
        source_copy.mkdir()
        for encoded_path in listed.stdout.split(b"\0"):
            if not encoded_path:
                continue
            relative_path = Path(os.fsdecode(encoded_path))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise HarnessError("git returned an unsafe tracked release path")
            if not (
                relative_path.as_posix() in TRACKED_RELEASE_FILES
                or (
                    relative_path.parts
                    and relative_path.parts[0] in TRACKED_RELEASE_DIRECTORIES
                )
            ):
                continue
            # Never inspect or package runtime data or local env files, even if
            # a future worktree accidentally tracks one.
            if (
                (relative_path.parts and relative_path.parts[0] == "data")
                or (
                    relative_path.name.startswith(".env")
                    and relative_path.name != ".env.example"
                )
            ):
                continue
            source_path = PROJECT_ROOT / relative_path
            if source_path.is_symlink():
                raise HarnessError("tracked release inputs may not be symlinks")
            if not source_path.is_file():
                continue
            destination = source_copy / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination, follow_symlinks=False)

        # The built frontend is the sole explicit untracked allowlist. Reject
        # symlinks so it cannot pull arbitrary local content into staging.
        frontend_dist = PROJECT_ROOT / "frontend" / "dist"
        if frontend_dist.is_dir():
            destination_dist = source_copy / "frontend" / "dist"
            destination_dist.mkdir(parents=True, exist_ok=True)
            for source_path in frontend_dist.rglob("*"):
                if source_path.is_symlink():
                    raise HarnessError("frontend/dist may not contain symlinks")
                relative_path = source_path.relative_to(frontend_dist)
                destination = destination_dist / relative_path
                if source_path.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif source_path.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, destination)
    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError("could not stage tracked release inputs") from exc

    builder = PROJECT_ROOT / "scripts" / "build-release.py"
    if not builder.is_file():
        return {
            "builder_present": False,
            "build_returncode": None,
            "archive_present": False,
            "start_script_count": None,
            "release_path": None,
        }
    if not Path(sys.executable).is_file():
        raise HarnessError("the Python harness interpreter is unavailable")
    archive_path = target / "learning-agent-release.tar"
    build_result = _run_harness_command(
        [
            sys.executable,
            str(builder),
            "--source",
            str(source_copy),
            "--output",
            str(archive_path),
        ],
        cwd=PROJECT_ROOT,
        env={
            "PATH": os.defpath,
            "LC_ALL": "C",
            "TZ": "UTC",
            "RELEASE_BUILD_OFFLINE": "1",
        },
        timeout=30,
    )
    if build_result.returncode != 0 or not archive_path.is_file():
        return {
            "builder_present": True,
            "build_returncode": build_result.returncode,
            "archive_present": archive_path.is_file(),
            "start_script_count": None,
            "release_path": None,
        }

    unpacked = target / "release-copy"
    try:
        shutil.unpack_archive(archive_path, unpacked, format="tar")
        start_scripts = list(unpacked.rglob("scripts/start.sh"))
    except (OSError, shutil.ReadError, ValueError) as exc:
        raise HarnessError("release builder produced an unreadable archive") from exc
    return {
        "builder_present": True,
        "build_returncode": 0,
        "archive_present": True,
        "start_script_count": len(start_scripts),
        "release_path": start_scripts[0].parents[1] if len(start_scripts) == 1 else None,
    }


def _initialize_ci_candidate(path: Path, included_gates: set[str]) -> Path:
    git = shutil.which("git")
    if git is None:
        raise HarnessError("git is required for the release-gate harness")
    unknown = included_gates.difference(REQUIRED_CI_GATES)
    if unknown:
        raise HarnessError(f"CI fixture contains unknown gates: {sorted(unknown)}")
    try:
        workflow = path / ".github" / "workflows" / "ci.yml"
        workflow.parent.mkdir(parents=True)
        scripts = path / "scripts"
        evidence = path / ".gate-evidence"
        scripts.mkdir()
        evidence.mkdir()
        (path / "README.md").write_text(
            "Synthetic release-gate candidate\n",
            encoding="utf-8",
        )
        (scripts / "fixture-gate.py").write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "gate = sys.argv[1]\n"
            "raise SystemExit(0 if (Path('.gate-evidence') / gate).is_file() else 3)\n",
            encoding="utf-8",
        )
        manifest = {
            "version": 1,
            "gates": {
                gate: ["python3", "scripts/fixture-gate.py", gate]
                for gate in REQUIRED_CI_GATES
                if gate in included_gates
            },
        }
        (path / "release-gates.json").write_text(
            json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )
        workflow_lines = ["name: Synthetic CI", "on: [push]", "jobs:"]
        for gate in REQUIRED_CI_GATES:
            if gate not in included_gates:
                continue
            (evidence / gate).write_text("pass\n", encoding="utf-8")
            workflow_lines.extend(
                [
                    f"  {gate}:",
                    "    runs-on: ubuntu-latest",
                    "    steps:",
                    f"      - run: python3 scripts/fixture-gate.py {gate}",
                ]
            )
        workflow.write_text("\n".join(workflow_lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise HarnessError("could not construct the synthetic CI candidate") from exc

    environment = {"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"}
    for command in (
        [git, "init", "--quiet"],
        [git, "add", "."],
        [
            git,
            "-c",
            "user.name=Release Gate Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "synthetic release candidate",
        ],
    ):
        result = _run_harness_command(
            command,
            cwd=path,
            env=environment,
            timeout=10,
        )
        if result.returncode != 0:
            raise HarnessError(f"could not initialize CI candidate: {result.stderr}")
    return path


def _call_release_gate(gate: Path, candidate: Path) -> tuple[int, dict]:
    if not Path(sys.executable).is_file():
        raise HarnessError("the Python harness interpreter is unavailable")
    result = _run_harness_command(
        [
            sys.executable,
            str(gate),
            "--repository",
            str(candidate),
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        env={
            "PATH": os.defpath,
            "LC_ALL": "C",
            "TZ": "UTC",
            "RELEASE_GATE_OFFLINE": "1",
        },
        timeout=20,
    )
    try:
        report = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HarnessError("release gate did not return its JSON contract") from exc
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("ok"), bool)
        or not isinstance(report.get("missing_gates"), list)
        or not all(isinstance(item, str) for item in report["missing_gates"])
    ):
        raise HarnessError("release gate returned a malformed JSON contract")
    return result.returncode, report


def test_reset_refuses_database_held_by_active_writer(tmp_path: Path):
    repository, environment = _synthetic_repository(tmp_path, "reset-data.sh")
    database_path = repository / "data" / "learning_companion.db"

    with _held_write_transaction(database_path):
        before = _sqlite_runtime_snapshot(database_path)
        result = _run_script(repository, environment, "reset-data.sh")
        after = _sqlite_runtime_snapshot(database_path)
        backup_side_effects = list((repository / "data").glob("backups/*"))

        assert result.returncode == 2
        assert _maintenance_error_code(result) == "active_sqlite_writer"
        assert database_path.is_file()
        assert after == before
        assert backup_side_effects == []


def test_reset_preserves_each_distinct_legacy_database(tmp_path: Path):
    repository, environment = _synthetic_repository(tmp_path, "reset-data.sh")
    source_paths = (
        repository / "data" / "learning_companion.db",
        repository / "learning_companion.db",
        repository / "backend" / "learning_companion.db",
    )
    for index, source_path in enumerate(source_paths):
        _create_database(source_path, f"source-{index}")
    expected_markers = {
        path.relative_to(repository).as_posix(): f"source-{index}"
        for index, path in enumerate(source_paths)
    }

    result = _run_script(repository, environment, "reset-data.sh")

    _require_harness(result.returncode == 0, f"legacy reset fixture failed: {result.stderr}")
    backup = _published_backup(repository, "pre_clean")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    sqlite_entries = {
        entry["source_path"]: entry
        for entry in manifest["entries"]
        if entry["kind"] == "sqlite"
    }
    assert set(sqlite_entries) == set(expected_markers)
    for relative_path, marker in expected_markers.items():
        entry = sqlite_entries[relative_path]
        assert entry["archive_path"] == f"payload/{relative_path}"
        assert _fixture_marker(backup / entry["archive_path"]) == (marker,)
        assert entry["sqlite"]["integrity_check"] == "ok"
        assert entry["sqlite"]["foreign_key_check_count"] == 0


def test_restore_rejects_corrupt_source_and_preserves_live_database(tmp_path: Path):
    repository, environment = _synthetic_repository(tmp_path, "seed-fixture.sh")
    live_database = repository / "data" / "learning_companion.db"
    _create_database(live_database, "known-good-live-state")
    live_hash_before = _sha256(live_database)

    corrupt_backup = (
        repository
        / "data"
        / "backups"
        / "pre-demo-20260818-000000"
        / "learning_companion.db"
    )
    _write_corrupt_database_fixture(corrupt_backup)

    result = _run_script(repository, environment, "seed-fixture.sh")

    assert result.returncode != 0
    assert _maintenance_error_code(result) in {"missing_backup", "manifest_missing"}
    assert live_database.is_file()
    assert _sha256(live_database) == live_hash_before
    assert _database_integrity(live_database) == [("ok",)]


def test_reset_output_can_be_restored_by_documented_restore_workflow(tmp_path: Path):
    repository, environment = _synthetic_repository(
        tmp_path,
        "reset-data.sh",
        "seed-fixture.sh",
    )
    live_database = repository / "data" / "learning_companion.db"
    _create_database(live_database, "roundtrip-state")

    reset_result = _run_script(repository, environment, "reset-data.sh")
    restore_result = _run_script(repository, environment, "seed-fixture.sh")

    _require_harness(
        reset_result.returncode == 0,
        f"documented reset fixture failed before restore: {reset_result.stderr}",
    )
    assert restore_result.returncode == 0, restore_result.stderr
    assert live_database.is_file()
    assert _fixture_marker(live_database) == ("roundtrip-state",)


@pytest.mark.parametrize(
    "failure_mode",
    ("active_writer", "legacy_path_flattening"),
    ids=("active-writer", "legacy-path-flattening"),
)
def test_demo_reset_preserves_live_and_legacy_sqlite_state(
    tmp_path: Path,
    failure_mode: str,
):
    repository, environment = _synthetic_repository(tmp_path, "demo-data.sh")
    live_database = repository / "data" / "learning_companion.db"

    if failure_mode == "active_writer":
        with _held_write_transaction(live_database):
            before = _sqlite_runtime_snapshot(live_database)
            result = _run_script(repository, environment, "demo-data.sh", "reset")
            after = _sqlite_runtime_snapshot(live_database)
            backup_side_effects = list((repository / "data").glob("backups/*"))

            assert result.returncode == 2
            assert _maintenance_error_code(result) == "active_sqlite_writer"
            assert after == before
            assert backup_side_effects == []
        return

    source_paths = (
        live_database,
        repository / "learning_companion.db",
        repository / "backend" / "learning_companion.db",
    )
    for index, source_path in enumerate(source_paths):
        _create_database(source_path, f"demo-source-{index}")
    expected_markers = {
        path.relative_to(repository).as_posix(): f"demo-source-{index}"
        for index, path in enumerate(source_paths)
    }

    result = _run_script(repository, environment, "demo-data.sh", "reset")

    _require_harness(result.returncode == 0, f"legacy demo reset fixture failed: {result.stderr}")
    backup = _published_backup(repository, "pre_demo")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    sqlite_entries = {
        entry["source_path"]: entry
        for entry in manifest["entries"]
        if entry["kind"] == "sqlite"
    }
    assert set(sqlite_entries) == set(expected_markers)
    for relative_path, marker in expected_markers.items():
        entry = sqlite_entries[relative_path]
        assert entry["archive_path"] == f"payload/{relative_path}"
        assert _fixture_marker(backup / entry["archive_path"]) == (marker,)


def test_demo_restore_rejects_corrupt_source_and_preserves_live_database(tmp_path: Path):
    repository, environment = _synthetic_repository(tmp_path, "demo-data.sh")
    live_database = repository / "data" / "learning_companion.db"
    _create_database(live_database, "known-good-demo-state")
    live_hash_before = _sha256(live_database)

    corrupt_backup = (
        repository
        / "data"
        / "backups"
        / "pre-demo-corrupt"
        / "learning_companion.db"
    )
    _write_corrupt_database_fixture(corrupt_backup)

    result = _run_script(
        repository,
        environment,
        "demo-data.sh",
        "restore",
        str(corrupt_backup.parent),
    )

    assert result.returncode != 0
    assert _maintenance_error_code(result) == "manifest_missing"
    assert live_database.is_file()
    assert _sha256(live_database) == live_hash_before
    assert _database_integrity(live_database) == [("ok",)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H8-BOOT-001: a fresh workspace with no model key opens the normal chat "
        "screen instead of entering the required first-run onboarding flow"
    ),
)
def test_fresh_workspace_enters_model_onboarding_before_first_session() -> None:
    _require_harness(FRONTEND_ROUTER.is_file(), "frontend router contract is missing")
    source = FRONTEND_ROUTER.read_text(encoding="utf-8")
    _require_harness(
        "{ path: '/', name: 'home'" in source,
        "fresh-workspace baseline cannot identify the normal home route",
    )
    assert "name: 'onboarding'" in source, (
        "fresh/no-key workspaces still have no dedicated onboarding route"
    )

@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H8-REL-001: the worktree has no callable release builder or runtime-asset "
        "packaging entrypoint"
    ),
)
def test_release_archive_starts_without_node_when_python_runtime_is_ready(tmp_path: Path):
    contract = _materialize_worktree_release(tmp_path)
    assert contract["builder_present"], "release builder entrypoint is missing"
    assert contract["build_returncode"] == 0
    assert contract["archive_present"]
    assert contract["start_script_count"] == 1
    release_path = contract["release_path"]
    if not isinstance(release_path, Path):
        raise HarnessError("release contract did not resolve one runtime root")

    dirname = shutil.which("dirname")
    bash = shutil.which("bash")
    if dirname is None or bash is None:
        raise HarnessError("bash and dirname are required for the release startup harness")
    try:
        fake_bin = release_path / "release-test-bin"
        fake_bin.mkdir()
        (fake_bin / "dirname").symlink_to(dirname)
        fake_curl = fake_bin / "curl"
        fake_curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
        fake_curl.chmod(0o755)

        runtime_python = release_path / ".venv" / "bin" / "python"
        runtime_python.parent.mkdir(parents=True)
        runtime_python.write_text(
            '#!/bin/sh\n: > "$PWD/runtime-started"\nexit 0\n',
            encoding="utf-8",
        )
        runtime_python.chmod(0o755)
        # This is inside the synthetic release only and contains no secret.
        (release_path / ".env").write_text("", encoding="utf-8")
    except OSError as exc:
        raise HarnessError("could not prepare the no-Node release runtime") from exc

    result = _run_harness_command(
        [bash, str(release_path / "scripts" / "start.sh")],
        cwd=release_path,
        env={"PATH": str(fake_bin), "LC_ALL": "C", "TZ": "UTC"},
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert (release_path / "runtime-started").is_file()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H8-REL-002: start.sh invokes npm during release startup when the packaged "
        "frontend asset is missing"
    ),
)
def test_release_start_never_builds_a_missing_frontend_asset(tmp_path: Path):
    repository, environment = _synthetic_repository(tmp_path, "start.sh")
    npm_marker = repository / "npm-was-invoked"
    runtime_marker = repository / "runtime-was-started"
    try:
        fake_npm = repository / "fake-bin" / "npm"
        fake_npm.write_text(
            '#!/bin/sh\n: > "$H0_NPM_MARKER"\nexit 0\n',
            encoding="utf-8",
        )
        fake_npm.chmod(0o755)
        runtime_python = repository / ".venv" / "bin" / "python"
        runtime_python.parent.mkdir(parents=True)
        runtime_python.write_text(
            '#!/bin/sh\n: > "$H0_RUNTIME_MARKER"\nexit 0\n',
            encoding="utf-8",
        )
        runtime_python.chmod(0o755)
        # Synthetic release fixture only; it contains no credential.
        (repository / ".env").write_text("", encoding="utf-8")
    except OSError as exc:
        raise HarnessError("could not prepare the release-start fixture") from exc

    environment.update(
        {
            "H0_NPM_MARKER": str(npm_marker),
            "H0_RUNTIME_MARKER": str(runtime_marker),
        }
    )
    result = _run_script(repository, environment, "start.sh")

    assert not npm_marker.exists(), "release startup invoked npm"
    assert result.returncode != 0
    assert "missing_release_asset" in result.stderr
    assert not runtime_marker.exists()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H8-CI-001: there is no executable release-gate entrypoint that can "
        "reject a candidate repository missing the mandatory CI gates"
    ),
)
def test_release_gate_rejects_repository_missing_mandatory_ci_checks(tmp_path: Path):
    gate = PROJECT_ROOT / "scripts" / "release-gate.py"
    assert gate.is_file(), "missing callable release gate: scripts/release-gate.py"

    complete = _initialize_ci_candidate(
        tmp_path / "complete-release-candidate",
        set(REQUIRED_CI_GATES),
    )
    returncode, report = _call_release_gate(gate, complete)
    assert returncode == 0
    assert report["ok"] is True
    assert report["missing_gates"] == []

    for missing_gate in REQUIRED_CI_GATES:
        candidate = _initialize_ci_candidate(
            tmp_path / f"missing-{missing_gate}",
            set(REQUIRED_CI_GATES).difference({missing_gate}),
        )
        returncode, report = _call_release_gate(gate, candidate)
        assert returncode != 0
        assert report["ok"] is False
        assert set(report["missing_gates"]) == {missing_gate}
