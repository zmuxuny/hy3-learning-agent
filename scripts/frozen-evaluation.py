#!/usr/bin/env python3
"""Verify frozen evaluation files, then test their original competition snapshot."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPETITION_SNAPSHOT = "7f31816b29bbb35390343db032af889d67efb0e7"


def _environment() -> dict[str, str]:
    # Do not inherit credentials, PYTHONPATH, pytest selection/plugins, or Git
    # overrides from the current product checkout. No .env is copied or loaded.
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", str(PROJECT_ROOT)),
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONUNBUFFERED": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "OPENAI_API_KEY": "",
        "OPENAI_API_BASE": "https://127.0.0.1:9/v1",
        "ENABLE_SCHEDULER": "false",
        "ENABLE_EMAIL_REPLY_POLLING": "false",
    }


def _git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=repository, env=_environment(), capture_output=True, timeout=120, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"frozen_evaluation_git_failed:{arguments[0]}:{result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def verify_evaluation(repository: Path) -> None:
    # This is deliberately a full commit ID, with no fetch or fallback to HEAD.
    _git(repository, "cat-file", "-e", f"{COMPETITION_SNAPSHOT}^{{commit}}")
    expected = {}
    for entry in _git(repository, "ls-tree", "-r", "-z", COMPETITION_SNAPSHOT, "--", "evaluation/").split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, kind, digest = metadata.split()
        if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
            raise RuntimeError("frozen_evaluation_unsupported_file_type")
        expected[path] = (mode, digest)
    if not expected:
        raise RuntimeError("frozen_evaluation_empty_snapshot")

    if _git(repository, "ls-files", "--others", "--exclude-standard", "-z", "--", "evaluation/"):
        raise RuntimeError("frozen_evaluation_untracked_files")
    actual = {}
    for entry in _git(repository, "ls-files", "--stage", "-z", "--", "evaluation/").split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, digest, stage = metadata.split()
        if stage != b"0":
            raise RuntimeError("frozen_evaluation_unmerged_index")
        actual[path] = (mode, digest)
    # Compare index objects too: staging tampered bytes and restoring only the
    # worktree must not make the mandatory historical gate pass.
    if actual != expected:
        raise RuntimeError("frozen_evaluation_index_mismatch")

    for encoded, (mode, digest) in expected.items():
        relative = Path(os.fsdecode(encoded))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("frozen_evaluation_unsafe_path")
        path = repository / relative
        for parent in relative.parents:
            if not stat.S_ISDIR((repository / parent).lstat().st_mode):
                raise RuntimeError(f"frozen_evaluation_file_type_mismatch:{relative}")
        file_mode = path.lstat().st_mode
        if mode == b"120000":
            if not stat.S_ISLNK(file_mode):
                raise RuntimeError(f"frozen_evaluation_file_type_mismatch:{relative}")
            data = os.fsencode(os.readlink(path))
        else:
            if not stat.S_ISREG(file_mode):
                raise RuntimeError(f"frozen_evaluation_file_type_mismatch:{relative}")
            if bool(file_mode & stat.S_IXUSR) != (mode == b"100755"):
                raise RuntimeError(f"frozen_evaluation_file_mode_mismatch:{relative}")
            data = path.read_bytes()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest().encode()
        if blob != digest:
            raise RuntimeError(f"frozen_evaluation_file_bytes_mismatch:{relative}")


def run(repository: Path = PROJECT_ROOT) -> None:
    repository = repository.resolve(strict=True)
    verify_evaluation(repository)
    with tempfile.TemporaryDirectory(prefix="hy3-frozen-evaluation-") as temporary:
        checkout = Path(temporary) / "snapshot"
        try:
            _git(repository, "worktree", "add", "--detach", str(checkout), COMPETITION_SNAPSHOT)
            if _git(checkout, "rev-parse", "HEAD").decode().strip() != COMPETITION_SNAPSHOT:
                raise RuntimeError("frozen_evaluation_snapshot_mismatch")
            print(f"Frozen evaluation snapshot: {COMPETITION_SNAPSHOT}", flush=True)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "evaluation/tests"],
                cwd=checkout, env=_environment(), timeout=7200, check=False,
            )
            if result.returncode:
                raise RuntimeError(f"frozen_evaluation_tests_failed:{result.returncode}")
        finally:
            if checkout.exists():
                _git(repository, "worktree", "remove", "--force", str(checkout))


def main() -> int:
    try:
        run()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
