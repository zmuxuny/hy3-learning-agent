#!/usr/bin/env python3
"""Validate and execute the mandatory release gates for one clean repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


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
GATE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def _report(*, ok: bool, missing: list[str], failures: list[dict], error: str = "") -> dict:
    return {
        "ok": ok,
        "missing_gates": missing,
        "failures": failures,
        "error": error,
    }


def _load_manifest(repository: Path) -> tuple[dict[str, list[str]], str]:
    try:
        raw = json.loads((repository / "release-gates.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, "invalid_release_gate_manifest"
    gates = raw.get("gates") if isinstance(raw, dict) and raw.get("version") == 1 else None
    if not isinstance(gates, dict):
        return {}, "invalid_release_gate_manifest"
    normalized: dict[str, list[str]] = {}
    for name, command in gates.items():
        if not isinstance(name, str) or not GATE_NAME.fullmatch(name):
            return {}, "invalid_release_gate_name"
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item and "\x00" not in item for item in command)
        ):
            return {}, f"invalid_release_gate_command:{name}"
        normalized[name] = command
    return normalized, ""


def _workflow_gates(repository: Path) -> set[str]:
    try:
        source = (repository / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    except OSError:
        return set()
    return {
        gate for gate in REQUIRED_GATES
        if re.search(rf"^  {re.escape(gate)}:\s*$", source, flags=re.MULTILINE)
    }


def _git_clean(repository: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def run(repository: Path) -> tuple[int, dict]:
    try:
        repository = repository.resolve(strict=True)
    except OSError:
        return 2, _report(ok=False, missing=list(REQUIRED_GATES), failures=[], error="repository_not_found")
    if not (repository / ".git").exists():
        return 2, _report(ok=False, missing=list(REQUIRED_GATES), failures=[], error="repository_not_git")
    commands, error = _load_manifest(repository)
    workflow = _workflow_gates(repository)
    missing = [gate for gate in REQUIRED_GATES if gate not in commands or gate not in workflow]
    if error or missing:
        return 2, _report(ok=False, missing=missing, failures=[], error=error)
    if set(commands) != set(REQUIRED_GATES):
        return 2, _report(ok=False, missing=[], failures=[], error="unexpected_release_gate")
    if not _git_clean(repository):
        return 2, _report(ok=False, missing=[], failures=[], error="release_repository_not_clean")

    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", str(repository)),
        "LC_ALL": "C",
        "TZ": "UTC",
        "PYTHONUNBUFFERED": "1",
        "RELEASE_GATE_OFFLINE": os.environ.get("RELEASE_GATE_OFFLINE", "0"),
    }
    failures = []
    for gate in REQUIRED_GATES:
        command = [sys.executable if item == "{python}" else item for item in commands[gate]]
        try:
            result = subprocess.run(
                command,
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append({"gate": gate, "returncode": None, "error": type(exc).__name__})
            break
        if result.returncode != 0:
            failures.append({
                "gate": gate,
                "returncode": result.returncode,
                "stderr_tail": result.stderr[-2000:],
            })
            break
    return (1 if failures else 0), _report(ok=not failures, missing=[], failures=failures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--format", choices=("json",), default="json")
    arguments = parser.parse_args()
    returncode, report = run(arguments.repository)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
