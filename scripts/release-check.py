#!/usr/bin/env python3
"""Run one named, repository-local release verification gate."""

from __future__ import annotations

import argparse
import compileall
import hashlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATES = (
    "lint_typecheck",
    "python_dependency_audit",
    "historical_migration",
    "evidence_audit",
    "context_isolation",
    "browser_matrix",
    "secret_scan",
    "frontend_build",
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{32,}"),
)


def _run(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    cwd: Path = PROJECT_ROOT,
    timeout: int = 3600,
) -> None:
    result = subprocess.run(command, cwd=cwd, env=environment, timeout=timeout, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"release_gate_command_failed:{command[0]}:{result.returncode}")


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("secret_scan_git_inventory_failed")
    files = []
    for encoded in result.stdout.split(b"\0"):
        if not encoded:
            continue
        relative = Path(os.fsdecode(encoded))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("secret_scan_unsafe_path")
        files.append(relative)
    return files


def lint_typecheck() -> None:
    if not compileall.compile_dir(PROJECT_ROOT / "backend", quiet=1):
        raise RuntimeError("python_compile_failed")
    if not compileall.compile_dir(PROJECT_ROOT / "evaluation" / "src", quiet=1):
        raise RuntimeError("evaluation_compile_failed")
    if not compileall.compile_dir(PROJECT_ROOT / "scripts", quiet=1):
        raise RuntimeError("script_compile_failed")
    _run([
        sys.executable, "-m", "ruff", "check", "--select", "E9,F63,F7,F82",
        "backend", "evaluation/src", "evaluation/tests", "scripts", "tests",
    ])
    for relative in _tracked_files():
        if relative.suffix in {".js", ".mjs"}:
            _run(["node", "--check", str(relative)], timeout=60)


def python_dependency_audit() -> None:
    _run([sys.executable, "-m", "pip", "check"], timeout=120)
    _run([
        sys.executable, "-m", "pip_audit", "--requirement", "backend/requirements-dev.txt",
    ], timeout=600)


def historical_migration() -> None:
    # The historical migration contracts are part of the complete backend
    # suite. Running the whole suite here preserves the pre-H8 CI regression
    # boundary while keeping the release manifest at exactly eight gates.
    _run([sys.executable, "-m", "pytest", "-q"])


def evidence_audit() -> None:
    _run([
        sys.executable, "-m", "pytest", "-q",
        "tests/hardening/test_h4_scenario_matrix.py",
        "tests/hardening/test_h4_projection_rebuild.py",
        "tests/hardening/test_h0_evidence_regressions.py",
        "tests/hardening/test_h4_evidence_runtime_failpoints.py",
        "tests/hardening/test_h4_policy_artifact_supersession.py",
    ])


def context_isolation() -> None:
    _run([
        sys.executable, "-m", "pytest", "-q",
        "tests/hardening/test_h0_context_memory_regressions.py",
        "tests/hardening/test_h5_context_protocol.py",
        "tests/hardening/test_h5_memory_protocol.py",
    ])


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _prepare_browser_candidate(destination: Path) -> None:
    shutil.copytree(
        PROJECT_ROOT / "backend",
        destination / "backend",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (destination / "scripts").mkdir()
    for name in ("h7_browser_fixture.py", "h7_browser_check.mjs", "h8_onboarding_browser_check.mjs"):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, destination / "scripts" / name)
    shutil.copytree(PROJECT_ROOT / "frontend/dist", destination / "frontend/dist")
    (destination / "frontend/node_modules").symlink_to(PROJECT_ROOT / "frontend/node_modules", target_is_directory=True)


def _wait_for_browser_server(server: subprocess.Popen[str], health: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server.poll() is not None:
            stderr = server.stderr.read()[-2000:] if server.stderr else ""
            raise RuntimeError(f"browser_server_failed:{stderr}")
        try:
            with urllib.request.urlopen(health, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError("browser_server_timeout")


def _stop_browser_server(server: subprocess.Popen[str]) -> None:
    if server.poll() is not None:
        return
    os.killpg(server.pid, signal.SIGTERM)
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(server.pid, signal.SIGKILL)
        server.wait(timeout=5)


def browser_matrix() -> None:
    if not (PROJECT_ROOT / "frontend/dist/index.html").is_file():
        raise RuntimeError("missing_release_asset")
    token = "h8-release-browser-fixture"
    with tempfile.TemporaryDirectory(prefix="learning-agent-h8-browser-") as temporary:
        temporary_path = Path(temporary)
        candidate = temporary_path / "candidate"
        _prepare_browser_candidate(candidate)
        database = candidate / "data/learning_companion.db"
        report = temporary_path / "report"
        port = _available_port()
        environment = {
            **os.environ,
            "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
            "OPENAI_API_KEY": "synthetic-browser-fixture-key",
            "ENABLE_SCHEDULER": "false",
            "DEPLOYMENT_MODE": "local",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }

        onboarding_candidate = temporary_path / "onboarding-candidate"
        _prepare_browser_candidate(onboarding_candidate)
        onboarding_environment = {
            **environment,
            "DATABASE_URL": f"sqlite+aiosqlite:///{onboarding_candidate / 'data/learning_companion.db'}",
            "OPENAI_API_KEY": "",
        }
        onboarding_port = _available_port()
        onboarding_server = subprocess.Popen(
            [sys.executable, "backend/run.py", "--host", "127.0.0.1", "--port", str(onboarding_port)],
            cwd=onboarding_candidate,
            env=onboarding_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            onboarding_url = f"http://127.0.0.1:{onboarding_port}"
            _wait_for_browser_server(onboarding_server, f"{onboarding_url}/api/v1/health")
            _run(
                ["node", "scripts/h8_onboarding_browser_check.mjs", onboarding_url],
                environment=onboarding_environment,
                cwd=onboarding_candidate,
                timeout=180,
            )
        finally:
            _stop_browser_server(onboarding_server)

        _run(
            [sys.executable, "scripts/h7_browser_fixture.py", token],
            environment=environment,
            cwd=candidate,
            timeout=180,
        )
        server = subprocess.Popen(
            [sys.executable, "backend/run.py", "--host", "127.0.0.1", "--port", str(port)],
            cwd=candidate,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            health = f"http://127.0.0.1:{port}/api/v1/health"
            _wait_for_browser_server(server, health)
            _run([
                "node", "scripts/h7_browser_check.mjs", f"http://127.0.0.1:{port}", token, str(report),
            ], environment=environment, cwd=candidate, timeout=600)
            if not (report / "h7-browser-report.json").is_file():
                raise RuntimeError("browser_report_missing")
        finally:
            _stop_browser_server(server)


def secret_scan() -> None:
    violations = []
    tracked = _tracked_files()
    for relative in tracked:
        if relative.name in {".env", ".env.local"}:
            violations.append(relative.as_posix())
            continue
        path = PROJECT_ROOT / relative
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        data = path.read_bytes()
        if b"\x00" not in data and any(pattern.search(data) for pattern in SECRET_PATTERNS):
            violations.append(relative.as_posix())
    if violations:
        raise RuntimeError(f"secret_scan_failed:{','.join(sorted(violations))}")
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists() and not env_path.is_file():
        raise RuntimeError("unsafe_local_env_path")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if staged.returncode != 0:
        raise RuntimeError("secret_scan_staged_diff_failed")
    digest = hashlib.sha256(staged.stdout).hexdigest()
    print(f"secret_scan_ok tracked={len(tracked)} staged_diff_sha256={digest}")


def frontend_build() -> None:
    _run(["npm", "--prefix", "frontend", "test"], timeout=600)
    _run(["npm", "--prefix", "frontend", "run", "build"], timeout=600)
    _run(["npm", "--prefix", "frontend", "audit", "--omit=dev"], timeout=600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=GATES)
    arguments = parser.parse_args()
    try:
        globals()[arguments.gate]()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
