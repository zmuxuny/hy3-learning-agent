"""Fail-closed normalization and stable logical identities for E2 exports."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .canonical import canonical_json_bytes

_PROHIBITED_KEYS = {
    "address",
    "api_key",
    "auth_token",
    "authorization",
    "claim_token",
    "credentials",
    "email",
    "endpoint",
    "keys",
    "password",
    "phone",
    "private_key",
    "push_key",
    "recipient",
    "reply_token",
    "routing",
    "secret",
    "smtp_from",
    "smtp_to",
    "token",
}


class NormalizationError(ValueError):
    """A public value cannot be normalized without guessing or leaking data."""

    def __init__(self, code: str, path: str):
        super().__init__(f"{code}:{path}")
        self.code = code
        self.path = path


def utc_timestamp(value: datetime) -> str:
    """Return one deterministic, microsecond-precision UTC timestamp."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def normalize_rfc3339(value: str) -> str:
    """Parse one offset-aware RFC3339 string and emit canonical UTC."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise NormalizationError("normalizer.invalid_timestamp", "$") from exc
    if parsed.tzinfo is None:
        raise NormalizationError("normalizer.naive_timestamp", "$")
    return utc_timestamp(parsed)


def _key_name(value: str) -> str:
    return "_".join(
        part for part in value.casefold().replace("-", "_").split("_") if part
    )


def normalize_json(value: object, *, path: str = "$") -> Any:
    """Normalize public JSON-like data and reject every unsupported type.

    Mapping keys are NFC strings, semantic sequences retain their order, and
    actual set values are sorted by the repository's canonical JSON bytes.
    Secret/routing-shaped keys fail closed instead of being silently dropped.
    """

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NormalizationError("normalizer.non_finite", path)
        if value == 0:
            return 0
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, datetime):
        return utc_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise NormalizationError("normalizer.non_string_key", path)
            key = unicodedata.normalize("NFC", raw_key)
            if _key_name(key) in _PROHIBITED_KEYS:
                raise NormalizationError("normalizer.prohibited_field", f"{path}.{key}")
            if key in normalized:
                raise NormalizationError("normalizer.duplicate_key", f"{path}.{key}")
            normalized[key] = normalize_json(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, AbstractSet) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        normalized_set = [
            normalize_json(item, path=f"{path}[]") for item in value
        ]
        return sorted(normalized_set, key=canonical_json_bytes)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            normalize_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise NormalizationError("normalizer.unsupported_type", path)


@dataclass(frozen=True, slots=True)
class IdentityCandidate:
    raw_id: object
    semantic_key: object


class StableIdentityRegistry:
    """Map private database identities to stable, scoped logical IDs.

    Declared fixture logical IDs are identity hints from runtime input, not
    expected results. Undeclared rows receive deterministic ordinals based on
    an allowlisted semantic key. Duplicate semantic keys fail closed because
    assigning different identities would otherwise depend on database order.
    """

    def __init__(
        self,
        *,
        episode_id: str,
        declarations: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.episode_id = episode_id
        self._declarations = {
            entity_type: tuple(values)
            for entity_type, values in (declarations or {}).items()
        }
        self._identities: dict[tuple[str, str], str] = {}
        self._next_ordinal: dict[str, int] = {}

    @staticmethod
    def _raw_key(entity_type: str, raw_id: object) -> tuple[str, str]:
        if raw_id is None:
            raise NormalizationError("identity.missing_raw_id", f"$.{entity_type}")
        if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool):
            raise NormalizationError("identity.unsupported_raw_id", f"$.{entity_type}")
        return entity_type, str(raw_id)

    def register_many(
        self,
        entity_type: str,
        candidates: Sequence[IdentityCandidate],
    ) -> None:
        pending = [
            candidate
            for candidate in candidates
            if self._raw_key(entity_type, candidate.raw_id) not in self._identities
        ]
        keyed = [
            (canonical_json_bytes(normalize_json(candidate.semantic_key)), candidate)
            for candidate in pending
        ]
        keyed.sort(key=lambda item: item[0])
        for index in range(1, len(keyed)):
            if keyed[index - 1][0] == keyed[index][0]:
                raise NormalizationError(
                    "identity.ambiguous_semantic_key", f"$.{entity_type}"
                )
        declarations = self._declarations.get(entity_type, ())
        next_ordinal = self._next_ordinal.get(entity_type, 1)
        used = set(self._identities.values())
        for _, candidate in keyed:
            while next_ordinal <= len(declarations) and declarations[
                next_ordinal - 1
            ] in used:
                next_ordinal += 1
            if next_ordinal <= len(declarations):
                logical_id = declarations[next_ordinal - 1]
            else:
                logical_id = (
                    f"{entity_type}:{self.episode_id}:"
                    f"{next_ordinal:03d}"
                )
            self._identities[self._raw_key(entity_type, candidate.raw_id)] = logical_id
            used.add(logical_id)
            next_ordinal += 1
        self._next_ordinal[entity_type] = next_ordinal

    def resolve(self, entity_type: str, raw_id: object | None) -> str | None:
        if raw_id is None:
            return None
        key = self._raw_key(entity_type, raw_id)
        try:
            return self._identities[key]
        except KeyError as exc:
            raise NormalizationError(
                "identity.unregistered_reference", f"$.{entity_type}"
            ) from exc

    def resolve_compound(self, entity_type: str, value: object) -> str:
        """Replace a raw identity prefix while preserving a public suffix.

        Production evidence keys such as ``<submission-id>:check`` are durable
        protocol identities, but their prefix may be a database-generated ID.
        Matching is exact or ``:`` delimited; ambiguous and unknown values fail
        closed instead of leaking the original identifier.
        """

        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise NormalizationError(
                "identity.unsupported_compound_reference", f"$.{entity_type}"
            )
        text = str(value)
        matches: list[tuple[str, str]] = []
        for (registered_type, raw_id), logical_id in self._identities.items():
            if registered_type != entity_type:
                continue
            if text == raw_id:
                matches.append((logical_id, ""))
            elif text.startswith(f"{raw_id}:"):
                matches.append((logical_id, text[len(raw_id) :]))
        if len(matches) != 1:
            raise NormalizationError(
                "identity.unregistered_compound_reference", f"$.{entity_type}"
            )
        logical_id, suffix = matches[0]
        return f"{logical_id}{suffix}"

    def contains(self, entity_type: str, raw_id: object) -> bool:
        """Return whether an allowlisted raw identity has been registered."""

        return self._raw_key(entity_type, raw_id) in self._identities

    def declared(self, entity_type: str) -> tuple[str, ...]:
        return self._declarations.get(entity_type, ())
