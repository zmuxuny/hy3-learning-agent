"""One canonical JSON and SHA-256 implementation for all evaluation artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class CanonicalizationError(ValueError):
    """The supplied value cannot be represented by the evaluation JSON contract."""


def _normalize(value: object, *, path: str) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"non-finite JSON number at {path}")
        if value == 0:
            return 0
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalizationError(f"non-string JSON object key at {path}")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise CanonicalizationError(f"duplicate normalized JSON key at {path}")
            normalized[key] = _normalize(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise CanonicalizationError(
        f"unsupported JSON value type at {path}: {type(value).__name__}"
    )


def canonical_json_bytes(value: object) -> bytes:
    """Return canonical UTF-8 JSON bytes for a JSON-shaped value.

    Object key order and insignificant source whitespace never affect the
    result. Strings and keys use NFC Unicode, integral floats collapse to
    integers, negative zero becomes zero, and non-finite or non-JSON values
    fail closed. Array order remains significant.
    """

    normalized = _normalize(value, path="$")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json(value: object) -> str:
    """Return :func:`canonical_json_bytes` decoded as UTF-8 text."""

    return canonical_json_bytes(value).decode("utf-8")


def sha256_digest(value: object) -> str:
    """Return lowercase SHA-256 hex over the canonical JSON bytes of ``value``."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
