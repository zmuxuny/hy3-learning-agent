from __future__ import annotations

import hashlib


BIT_WIDTH = 64


def simhash(tokens: list[str]) -> int:
    """Compute a 64-bit SimHash fingerprint from token strings."""
    weights = [0] * BIT_WIDTH
    for token in tokens:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        value = int.from_bytes(digest[:8], "big")
        for bit in range(BIT_WIDTH):
            weights[bit] += 1 if (value >> bit) & 1 else -1
    fingerprint = 0
    for bit in range(BIT_WIDTH):
        if weights[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def similarity(left: int, right: int) -> float:
    return 1.0 - hamming_distance(left, right) / BIT_WIDTH
