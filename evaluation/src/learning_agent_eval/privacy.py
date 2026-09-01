"""Fail-closed privacy checks that never load application configuration."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .errors import ValidationIssue

_PRIVATE_REASONING_KEYS = {
    "analysis",
    "chain_of_thought",
    "cot",
    "deliberation",
    "internal_analysis",
    "internal_thoughts",
    "model_thoughts",
    "private_reasoning",
    "reasoning",
    "reasoning_content",
    "scratchpad",
    "thought_process",
    "thinking_content",
}
_CREDENTIAL_KEYS = {
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "bearer",
    "bearer_token",
    "cookie",
    "credentials",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "set_cookie",
    "token",
}
_CREDENTIAL_SUFFIXES = (
    "_api_key",
    "_auth_token",
    "_authorization",
    "_bearer_token",
    "_cookie",
    "_credentials",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
)
_PII_KEYS = {
    "address",
    "email",
    "email_address",
    "full_name",
    "government_id",
    "id_card",
    "imap_username",
    "national_id",
    "passport_number",
    "phone",
    "phone_number",
    "real_name",
    "social_security_number",
    "smtp_from",
    "smtp_to",
    "smtp_username",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_IDENTITY_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
)
_SHA256_VALUE = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_REASONING_VALUE_PATTERNS = (
    re.compile(r"(?i)\breasoning(?:[_ -]content)?\b"),
    re.compile(r"(?i)\bchain[-_ ]of[-_ ]thought\b"),
    re.compile(
        r"(?i)\b(?:internal|private)[-_ ](?:analysis|deliberation|thoughts?)\b"
    ),
    re.compile(r"(?i)\b(?:scratchpad|thought[-_ ]process)\b"),
    re.compile(r"思维链|推理过程|内部推理"),
)
_RESERVED_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
}


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")


def is_private_reasoning_field(key: object) -> bool:
    """Identify private-reasoning field names without inspecting their values."""

    return _normalized_key(key) in _PRIVATE_REASONING_KEYS


def _child_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path != "$" else f"$.{key}"


def _is_real_email(value: str) -> bool:
    for match in _EMAIL_PATTERN.finditer(value):
        domain = match.group(0).rsplit("@", 1)[1].casefold()
        if (
            domain not in _RESERVED_EMAIL_DOMAINS
            and not domain.endswith(".invalid")
            and not domain.endswith(".test")
        ):
            return True
    return False


def privacy_issues(value: object, *, file: str = "<memory>") -> list[ValidationIssue]:
    """Return prohibited-field/value errors without returning rejected content."""

    issues: list[ValidationIssue] = []

    def visit(
        item: object, path: str, *, verified_digest_shape: bool = False
    ) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                child_path = _child_path(path, key)
                normalized = _normalized_key(key)
                if is_private_reasoning_field(key):
                    issues.append(
                        ValidationIssue(
                            code="privacy.private_reasoning",
                            path=child_path,
                            message="Private reasoning fields are prohibited.",
                            file=file,
                        )
                    )
                    continue
                if normalized in _CREDENTIAL_KEYS or normalized.endswith(
                    _CREDENTIAL_SUFFIXES
                ):
                    issues.append(
                        ValidationIssue(
                            code="privacy.credential_field",
                            path=child_path,
                            message="Credential-bearing fields are prohibited.",
                            file=file,
                        )
                    )
                    continue
                if normalized in _PII_KEYS:
                    issues.append(
                        ValidationIssue(
                            code="privacy.personal_identifier_field",
                            path=child_path,
                            message="Personal identity fields are prohibited.",
                            file=file,
                        )
                    )
                    continue
                visit(
                    child,
                    child_path,
                    verified_digest_shape=(
                        (
                            isinstance(child, str)
                            and _SHA256_VALUE.fullmatch(child) is not None
                            and (
                                verified_digest_shape
                                or normalized.endswith(("_digest", "_sha256"))
                            )
                        )
                        or (
                            isinstance(child, Mapping)
                            and normalized.endswith("_digests")
                        )
                    ),
                )
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
            return
        if not isinstance(item, str):
            return
        if any(pattern.search(item) for pattern in _PRIVATE_REASONING_VALUE_PATTERNS):
            issues.append(
                ValidationIssue(
                    code="privacy.private_reasoning_value",
                    path=path,
                    message="Private model-work text is prohibited.",
                    file=file,
                )
            )
        if any(pattern.search(item) for pattern in _SECRET_PATTERNS):
            issues.append(
                ValidationIssue(
                    code="privacy.credential_value",
                    path=path,
                    message="Credential-shaped text is prohibited.",
                    file=file,
                )
            )
        if _is_real_email(item):
            issues.append(
                ValidationIssue(
                    code="privacy.real_email",
                    path=path,
                    message="Non-reserved email addresses are prohibited.",
                    file=file,
                )
            )
        if not verified_digest_shape and any(
            pattern.search(item) for pattern in _IDENTITY_PATTERNS
        ):
            issues.append(
                ValidationIssue(
                    code="privacy.personal_identifier_value",
                    path=path,
                    message="Personal identifier-shaped text is prohibited.",
                    file=file,
                )
            )

    visit(value, "$")
    return issues
