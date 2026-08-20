"""Stable classification for model-provider failures safe to retry from a checkpoint."""

from __future__ import annotations


_TRANSIENT_STATUS_CODES = frozenset({408, 409, 425, 429})
_TRANSIENT_CLASS_NAMES = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
})


def is_transient_model_error(error: BaseException) -> bool:
    """Avoid retrying auth/request errors while tolerating compatible SDKs."""

    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code in _TRANSIENT_STATUS_CODES or status_code >= 500
    return type(error).__name__ in _TRANSIENT_CLASS_NAMES
