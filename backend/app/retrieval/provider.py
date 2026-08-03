from __future__ import annotations

from typing import Protocol

from app.core.config import settings
from app.retrieval.simhash import BIT_WIDTH, simhash
from app.retrieval.text import tokenize_terms


class EmbeddingProvider(Protocol):
    name: str
    dimension: int

    def embed(self, text: str) -> list[float]:
        ...


class LocalHashEmbedding:
    """Deterministic, offline 64-bit SimHash provider with zero extra dependencies."""

    name = "local_hash"
    dimension = BIT_WIDTH

    def embed(self, text: str) -> list[float]:
        fingerprint = simhash(tokenize_terms(text))
        return [float((fingerprint >> bit) & 1) for bit in range(BIT_WIDTH)]


def get_embedding_provider() -> EmbeddingProvider | None:
    if settings.MEMORY_RETRIEVAL_PROVIDER == "local_hash":
        return LocalHashEmbedding()
    return None


def embedding_similarity(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    differing = sum(a != b for a, b in zip(left, right))
    return max(0.0, 1.0 - differing / len(left))
