"""Fail-before-side-effect guard for retired evaluation executors."""

from __future__ import annotations


class LegacyExecutionDisabledError(RuntimeError):
    """A caller selected a frozen historical execution path."""

    code = "legacy_execution_disabled"

    def __init__(self, entrypoint: str):
        super().__init__(
            f"{entrypoint} is frozen for read-only validation; use the active "
            "Evaluation Protocol Release 1.0 entrypoint"
        )
        self.entrypoint = entrypoint

    def as_dict(self) -> dict[str, str]:
        return {
            "status": "error",
            "error_code": self.code,
            "stage": "preflight",
            "entrypoint": self.entrypoint,
            "message": str(self),
        }


def legacy_execution_disabled(entrypoint: str) -> None:
    """Raise before evaluating arguments or touching external state."""

    raise LegacyExecutionDisabledError(entrypoint)
