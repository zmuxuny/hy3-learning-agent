from __future__ import annotations

import hashlib
import math

import pytest
from learning_agent_eval.canonical import (
    CanonicalizationError,
    canonical_json,
    canonical_json_bytes,
    sha256_digest,
)


def test_canonical_json_and_digest_are_semantically_deterministic() -> None:
    first = {
        "z": -0.0,
        "e\u0301": 1.0,
        "list": ["e\u0301", 1.25],
    }
    second = {
        "list": ["é", 1.25],
        "é": 1,
        "z": 0,
    }

    expected = '{"list":["é",1.25],"z":0,"é":1}'
    expected_bytes = expected.encode("utf-8")
    assert canonical_json(first) == expected
    assert canonical_json(second) == expected
    assert canonical_json_bytes(first) == expected_bytes
    assert sha256_digest(first) == hashlib.sha256(expected_bytes).hexdigest()
    assert sha256_digest(second) == sha256_digest(first)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(CanonicalizationError, match="non-finite"):
        canonical_json({"value": value})


@pytest.mark.parametrize("value", [{1: "value"}, {"value": b"bytes"}, object()])
def test_canonical_json_rejects_non_json_values(value: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(value)


def test_canonical_json_rejects_unicode_normalization_key_collisions() -> None:
    with pytest.raises(CanonicalizationError, match="duplicate normalized"):
        canonical_json({"é": 1, "e\u0301": 2})
