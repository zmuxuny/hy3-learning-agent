"""Scoped entity-identity source with production-random defaults."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid4, uuid5


@dataclass
class _DeterministicSequence:
    seed: str
    ordinal: int = 0


_sequence: ContextVar[_DeterministicSequence | None] = ContextVar(
    "learning_agent_deterministic_identity_sequence",
    default=None,
)


def new_uuid_string() -> str:
    sequence = _sequence.get()
    if sequence is None:
        return str(uuid4())
    sequence.ordinal += 1
    return str(uuid5(NAMESPACE_URL, f"learning-agent-evaluation:{sequence.seed}:{sequence.ordinal}"))


@contextmanager
def deterministic_entity_ids(seed: str) -> Iterator[None]:
    """Generate repeatable ORM entity IDs only inside one explicit scope."""

    if not seed:
        raise ValueError("deterministic identity seed must not be empty")
    token = _sequence.set(_DeterministicSequence(seed=seed))
    try:
        yield
    finally:
        _sequence.reset(token)
