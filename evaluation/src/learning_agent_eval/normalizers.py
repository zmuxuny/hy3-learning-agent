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
        identity_bindings: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        self.episode_id = episode_id
        self._declarations = {
            entity_type: tuple(values)
            for entity_type, values in (declarations or {}).items()
        }
        self._identities: dict[tuple[str, str], str] = {}
        self._next_ordinal: dict[str, int] = {}
        self._bindings: dict[str, tuple[tuple[str, dict[str, object]], ...]] = {}
        self._resolved_binding_ids: set[str] = set()
        grouped: dict[str, list[tuple[str, dict[str, object]]]] = {}
        for binding in identity_bindings or ():
            entity_type = str(binding["entity_type"])
            logical_id = str(binding["logical_id"])
            fields = normalize_json(binding["identity_fields"])
            if not isinstance(fields, dict) or not fields:
                raise NormalizationError(
                    "identity.binding_fields_invalid", f"$.{entity_type}"
                )
            grouped.setdefault(entity_type, []).append((logical_id, fields))
        for entity_type, values in grouped.items():
            fingerprints = [canonical_json_bytes(fields) for _, fields in values]
            if len(fingerprints) != len(set(fingerprints)):
                raise NormalizationError(
                    "identity.ambiguous_binding", f"$.{entity_type}"
                )
            self._bindings[entity_type] = tuple(values)

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
        keyed = []
        for candidate in pending:
            semantic_key = normalize_json(candidate.semantic_key)
            if not isinstance(semantic_key, dict):
                raise NormalizationError(
                    "identity.semantic_key_invalid", f"$.{entity_type}"
                )
            keyed.append((canonical_json_bytes(semantic_key), candidate, semantic_key))
        keyed.sort(key=lambda item: item[0])
        for index in range(1, len(keyed)):
            if keyed[index - 1][0] == keyed[index][0]:
                raise NormalizationError(
                    "identity.ambiguous_semantic_key", f"$.{entity_type}"
                )
        declarations = self._declarations.get(entity_type, ())
        bindings = self._bindings.get(entity_type, ())
        next_ordinal = self._next_ordinal.get(
            entity_type, len(declarations) + 1 if bindings else 1
        )
        used = set(self._identities.values())
        unmatched: list[IdentityCandidate] = []
        for _, candidate, semantic_key in keyed:
            matches = [
                logical_id
                for logical_id, fields in bindings
                if all(
                    key in semantic_key
                    and canonical_json_bytes(semantic_key[key])
                    == canonical_json_bytes(value)
                    for key, value in fields.items()
                )
            ]
            if len(matches) > 1:
                raise NormalizationError(
                    "identity.ambiguous_binding", f"$.{entity_type}"
                )
            if matches:
                logical_id = matches[0]
                if logical_id in used:
                    raise NormalizationError(
                        "identity.binding_reused", f"$.{entity_type}"
                    )
                self._identities[
                    self._raw_key(entity_type, candidate.raw_id)
                ] = logical_id
                self._resolved_binding_ids.add(logical_id)
                used.add(logical_id)
            else:
                unmatched.append(candidate)
        reserved = set(declarations) if bindings else set()
        for candidate in unmatched:
            while next_ordinal <= len(declarations) and declarations[
                next_ordinal - 1
            ] in used | reserved:
                next_ordinal += 1
            if not bindings and next_ordinal <= len(declarations):
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

    def assert_bindings_resolved(self, entity_types: Sequence[str]) -> None:
        """Fail when a declared runtime entity was not found by its semantic key."""

        unresolved = sorted(
            logical_id
            for entity_type in entity_types
            for logical_id, _ in self._bindings.get(entity_type, ())
            if logical_id not in self._resolved_binding_ids
        )
        if unresolved:
            raise NormalizationError(
                "identity.binding_unresolved", f"$.{unresolved[0]}"
            )

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

    def is_registered(self, entity_type: str, raw_id: object) -> bool:
        """Return whether a raw identity already has a stable mapping."""

        return self._raw_key(entity_type, raw_id) in self._identities

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
