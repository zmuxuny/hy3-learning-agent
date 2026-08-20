from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from app.core.config import settings


REDACTED = "[REDACTED]"

_SECRET_SETTING_NAMES = (
    "SERVER_AUTH_TOKEN",
    "OPENAI_API_KEY",
    # Mailbox usernames and routes are credentials/personal identifiers even
    # when the protocol's authentication secret is stored separately.
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_TO",
    "IMAP_USERNAME",
    "IMAP_PASSWORD",
    "VAPID_PRIVATE_KEY",
    "VAPID_SUBJECT",
)
_SECRET_KEY_EXACT = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credentials",
    "password",
    "private_key",
    "secret",
    "set_cookie",
    "token",
}
_SECRET_KEY_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_cookie",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_token",
)
_QUOTED_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"'](?:api[_-]?key|authorization|cookie|credentials?|password|"
    r"private[_-]?key|secret|token|[a-z0-9_-]+[_-]token)[\"']\s*:\s*[\"'])"
    r"([^\"']*)([\"'])"
)
_PLAIN_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|credentials?|password|"
    r"private[_-]?key|secret|token|[a-z0-9_-]+[_-]token)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_CREDENTIAL = re.compile(
    r"(?i)\b((?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+)([^\s,;]+)"
)
def configured_secret_values() -> tuple[str, ...]:
    """Return current configured credentials without caching mutable settings."""

    values: list[str] = []
    for name in _SECRET_SETTING_NAMES:
        value = getattr(settings, name, "")
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return tuple(sorted(values, key=len, reverse=True))


def is_secret_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
    return normalized in _SECRET_KEY_EXACT or normalized.endswith(_SECRET_KEY_SUFFIXES)


def redact_text(value: object, *, secrets: tuple[str, ...] | None = None) -> str:
    """Redact configured values and credential-shaped text from one string."""

    text = str(value)
    secret_values = configured_secret_values() if secrets is None else secrets
    text = _redact_configured_literals(text, secret_values)
    text = _QUOTED_SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{REDACTED}{match.group(3)}",
        text,
    )
    text = _BEARER_CREDENTIAL.sub(
        lambda match: f"{match.group(1)}{REDACTED}",
        text,
    )
    return _PLAIN_SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        text,
    )


def _redact_configured_literals(value: object, secrets: tuple[str, ...]) -> str:
    """Redact configured literals without rewriting logging placeholders."""

    text = str(value)
    literal_secrets = tuple(
        sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    )
    if literal_secrets:
        # Configured credentials are authoritative regardless of length. A
        # short value can still be embedded in a provider error, validation
        # payload, or receipt; retaining any literal occurrence would violate
        # the redaction boundary. The possible loss of diagnostic text is the
        # deliberate fail-closed tradeoff for an explicitly configured secret.
        literal_pattern = re.compile(
            "|".join(re.escape(secret) for secret in literal_secrets)
        )
        text = literal_pattern.sub(REDACTED, text)
    return text


def redact_data(value: Any, *, secrets: tuple[str, ...] | None = None) -> Any:
    """Recursively redact JSON/log-shaped values while preserving structure."""

    secret_values = configured_secret_values() if secrets is None else secrets
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = redact_text(key, secrets=secret_values) if isinstance(key, str) else key
            redacted[safe_key] = (
                REDACTED
                if is_secret_key(key)
                else redact_data(item, secrets=secret_values)
            )
        return redacted
    if isinstance(value, list):
        return [redact_data(item, secrets=secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, secrets=secret_values) for item in value)
    if isinstance(value, set):
        return {redact_data(item, secrets=secret_values) for item in value}
    if isinstance(value, frozenset):
        return frozenset(redact_data(item, secrets=secret_values) for item in value)
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", errors="replace"), secrets=secret_values)
    if isinstance(value, str):
        return redact_text(value, secrets=secret_values)
    return value


def redact_event_fields(summary: object, payload: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Canonical boundary for persisted or streamed event fields."""

    secrets = configured_secret_values()
    safe_payload = redact_data(dict(payload or {}), secrets=secrets)
    return redact_text(summary, secrets=secrets), safe_payload


def redact_validation_errors(
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Redact FastAPI/Pydantic errors, including unconfigured secret inputs.

    Field validation stores the submitted value under the generic ``input``
    key, so recursive key matching alone cannot recognize a new API key or
    password. The location is the authority for that classification. For a
    secret field, retain only its location/type and a stable public message;
    the original input and validator context must not be reflected.
    """

    safe_errors: list[dict[str, Any]] = []
    for error in errors:
        safe_error = dict(error)
        location = safe_error.get("loc")
        location_parts = (
            tuple(location)
            if isinstance(location, (list, tuple))
            else (location,)
        )
        if any(is_secret_key(part) for part in location_parts if part is not None):
            if "input" in safe_error:
                safe_error["input"] = REDACTED
            safe_error.pop("ctx", None)
            safe_error["msg"] = "Secret field validation failed"
        safe_errors.append(redact_data(safe_error))
    return safe_errors


class SecretRedactionFilter(logging.Filter):
    """Logging filter that removes credentials before formatter/handler output."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = configured_secret_values()
        # Preserve the logging call's format template and argument shape.
        # Uvicorn's access formatter consumes the five positional arguments
        # directly instead of calling ``record.getMessage()``; eagerly
        # rendering and clearing them breaks every access log entry. Dynamic
        # values live in ``args`` and are recursively redacted. A template can
        # only contain static text plus %-placeholders, so redact configured
        # literal values without treating placeholders as credential values.
        if record.args:
            record.msg = _redact_configured_literals(record.msg, secrets)
            record.args = redact_data(record.args, secrets=secrets)
        else:
            record.msg = redact_text(record.msg, secrets=secrets)
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = redact_text(formatted, secrets=secrets)
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text, secrets=secrets)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, secrets=secrets)
        return True


def install_redaction_filter(logger: logging.Logger | None = None) -> SecretRedactionFilter:
    """Install process-wide record redaction plus handler-level defense."""

    target = logger or logging.getLogger()
    existing = next(
        (item for item in target.filters if isinstance(item, SecretRedactionFilter)),
        None,
    )
    redaction_filter = existing or SecretRedactionFilter()
    if existing is None:
        target.addFilter(redaction_filter)
    for handler in target.handlers:
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(redaction_filter)
    current_factory = logging.getLogRecordFactory()
    if not getattr(current_factory, "_learning_agent_redaction_factory", False):
        record_filter = SecretRedactionFilter()

        def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = current_factory(*args, **kwargs)
            record_filter.filter(record)
            return record

        setattr(redacting_factory, "_learning_agent_redaction_factory", True)
        logging.setLogRecordFactory(redacting_factory)
    return redaction_filter
