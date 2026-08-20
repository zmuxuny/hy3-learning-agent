"""Fail-closed policy for high-risk local code execution.

H6 deliberately does not treat the legacy ``prlimit`` host process as a
sandbox.  Until a provider can prove every required isolation capability,
``code_execute`` is unavailable at the tool surface, direct execution
boundary, and durable outbox delivery boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CODE_EXECUTION_POLICY_VERSION = "h6-code-execution-disabled-v1"
CODE_EXECUTION_ERROR_CODE = "code_execution_disabled"
CODE_EXECUTION_REASON_CODE = "SANDBOX_PROVIDER_UNAVAILABLE"
TRUSTED_SUBPROCESS_PATH = "/usr/bin:/bin"


@dataclass(frozen=True)
class CodeExecutionPolicy:
    deployment_mode: Literal["local", "server"]
    available: bool
    provider_id: str | None
    policy_version: str
    reason_code: str


def current_code_execution_policy(
    *, deployment_mode: Literal["local", "server"] | None = None
) -> CodeExecutionPolicy:
    """Return the immutable capability decision for this build.

    There is intentionally no environment-variable escape hatch.  Adding a
    real provider later must replace this decision with capability-attested
    configuration rather than silently re-enabling the host runner.
    """

    if deployment_mode is None:
        # Keep the import lazy: config owns deployment validation and imports
        # no execution-policy code, while callers may import this module during
        # settings construction.
        from app.core.config import settings

        deployment_mode = settings.DEPLOYMENT_MODE
    return CodeExecutionPolicy(
        deployment_mode=deployment_mode,
        available=False,
        provider_id=None,
        policy_version=CODE_EXECUTION_POLICY_VERSION,
        reason_code=CODE_EXECUTION_REASON_CODE,
    )


def code_execution_rejection() -> dict[str, object]:
    policy = current_code_execution_policy()
    return {
        "ok": False,
        "error": (
            "Code execution disabled: permission denied because no sandbox "
            "provider is available."
        ),
        "error_code": CODE_EXECUTION_ERROR_CODE,
        "reason_code": policy.reason_code,
        "policy_version": policy.policy_version,
        "retryable": False,
    }


def minimal_subprocess_environment() -> dict[str, str]:
    """Build a fixed environment without consulting the parent process."""

    return {
        "PATH": TRUSTED_SUBPROCESS_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }
