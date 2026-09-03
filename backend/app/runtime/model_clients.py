"""Runtime-wide model client seam and call metadata.

The production default still constructs the configured OpenAI-compatible
client.  Evaluation can install one process-scoped factory before any Runtime
or tool code executes, so child and auxiliary calls cannot bypass recording.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

ModelCallPurpose = Literal[
    "decision",
    "subagent_decision",
    "subagent_synthesis",
    "session_title",
    "memory_compression",
]


@dataclass(frozen=True, slots=True)
class ModelCallMetadata:
    run_id: str
    parent_run_id: str | None
    parent_call_id: str | None
    depth: int
    call_purpose: ModelCallPurpose
    decision_relevant: bool


_client_factory: ContextVar[Callable[[], Any] | None] = ContextVar(
    "runtime_model_client_factory",
    default=None,
)
_call_metadata: ContextVar[ModelCallMetadata | None] = ContextVar(
    "runtime_model_call_metadata",
    default=None,
)


def create_model_client() -> Any:
    """Create the process-scoped client without importing evaluation code."""

    factory = _client_factory.get()
    if factory is not None:
        return factory()

    from app.core.config import settings
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_API_BASE,
    )


@contextmanager
def use_model_client_factory(factory: Callable[[], Any]) -> Iterator[None]:
    """Install one client factory for the current async context tree."""

    token = _client_factory.set(factory)
    try:
        yield
    finally:
        _client_factory.reset(token)


@contextmanager
def model_call_scope(
    *,
    run_id: str,
    parent_run_id: str | None,
    call_purpose: ModelCallPurpose,
    decision_relevant: bool,
    depth: int,
    parent_call_id: str | None = None,
) -> Iterator[None]:
    """Attach stable hierarchy and purpose metadata to one provider call."""

    if depth < 0:
        raise ValueError("model call depth cannot be negative")
    token = _call_metadata.set(
        ModelCallMetadata(
            run_id=run_id,
            parent_run_id=parent_run_id,
            parent_call_id=parent_call_id,
            depth=depth,
            call_purpose=call_purpose,
            decision_relevant=decision_relevant,
        )
    )
    try:
        yield
    finally:
        _call_metadata.reset(token)


def current_model_call_metadata() -> ModelCallMetadata | None:
    """Return metadata for a recorder without exposing a mutable context."""

    return _call_metadata.get()
