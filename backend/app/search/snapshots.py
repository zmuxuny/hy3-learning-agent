"""Scoped resource-provider seam for deterministic, offline execution.

Production callers see ``None`` and retain the existing search/DNS/HTTP path.
An isolated evaluation worker can install a provider for one async execution;
unknown queries and URLs are then the provider's responsibility and cannot
fall through to a live transport.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol


class SnapshotResourceUnavailable(ValueError):
    """A query or URL is absent from the active immutable snapshot."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SnapshotSearchResult(Protocol):
    title: str
    url: str

    def as_dict(self) -> dict[str, str]: ...


class ResourceSnapshotProvider(Protocol):
    name: str

    async def search(self, query: str, limit: int) -> list[SnapshotSearchResult]: ...

    async def open(self, url: str, max_chars: int) -> dict[str, Any]: ...

    async def validate(self, url: str) -> None: ...


_active_snapshot_provider: ContextVar[ResourceSnapshotProvider | None] = ContextVar(
    "learning_agent_resource_snapshot_provider",
    default=None,
)


def current_snapshot_provider() -> ResourceSnapshotProvider | None:
    return _active_snapshot_provider.get()


@contextmanager
def use_snapshot_provider(
    provider: ResourceSnapshotProvider,
) -> Iterator[ResourceSnapshotProvider]:
    token = _active_snapshot_provider.set(provider)
    try:
        yield provider
    finally:
        _active_snapshot_provider.reset(token)
