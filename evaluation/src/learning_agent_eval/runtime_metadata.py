"""Stable E3.1 runtime metadata without importing product configuration."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .canonical import sha256_digest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEPENDENCY_LOCK_VERSION = "evaluation-runtime-dependencies-v1"
DEPENDENCY_FILES = (
    "backend/requirements.txt",
    "backend/requirements-dev.txt",
    "evaluation/pyproject.toml",
)
ENDPOINT_POLICY_VERSION = "hy3-endpoint-policy-v1"
HY3_ENDPOINT_ID = "tencent-tokenhub-primary"
HY3_ENDPOINT_ORIGIN = "https://tokenhub.tencentmaas.com"
HY3_API_BASE = f"{HY3_ENDPOINT_ORIGIN}/v1"
HY3_MODEL = "hy3"
ENDPOINT_POLICY_SHA256 = sha256_digest(
    {
        "version": ENDPOINT_POLICY_VERSION,
        "allowed_endpoints": [
            {
                "endpoint_id": HY3_ENDPOINT_ID,
                "origin": HY3_ENDPOINT_ORIGIN,
                "request_model": HY3_MODEL,
            }
        ],
    }
)


def dependency_lock_sha256(project_root: Path = PROJECT_ROOT) -> str:
    """Hash the exact committed dependency declarations in a framed stream."""

    digest = hashlib.sha256()
    for relative in DEPENDENCY_FILES:
        payload = (project_root / relative).read_bytes()
        encoded_name = relative.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def git_worktree_clean(project_root: Path = PROJECT_ROOT) -> bool:
    """Return the auditable Git cleanliness bit or fail instead of guessing."""

    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git_status_unavailable")
    return not completed.stdout
