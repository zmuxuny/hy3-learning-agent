"""Immutable public/synthetic resource snapshots for E1 workers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from .canonical import sha256_digest


@dataclass(frozen=True)
class SnapshotSearchResult:
    title: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url}


class EvaluationSnapshotProvider:
    name = "decisionbench-e1-snapshot"

    def __init__(self, manifest_path: str | Path):
        path = Path(manifest_path)
        document = json.loads(path.read_text(encoding="utf-8"))
        expected = document.get("manifest_sha256")
        payload = {key: value for key, value in document.items() if key != "manifest_sha256"}
        if expected != sha256_digest(payload):
            raise ValueError("resource snapshot manifest digest mismatch")
        self.document = document
        self.digest = str(expected)
        self.version = str(document["snapshot_version"])
        self._queries = {item["query"]: item for item in document["queries"]}
        self._pages = {item["url"]: item for item in document["pages"]}
        self.calls = {"search": 0, "open": 0, "validate": 0}

    @staticmethod
    def _unavailable(kind: str) -> Exception:
        error = import_module("app.search.snapshots").SnapshotResourceUnavailable
        return error(f"snapshot_{kind}_not_registered")

    async def search(self, query: str, limit: int) -> list[SnapshotSearchResult]:
        self.calls["search"] += 1
        entry = self._queries.get(query)
        if entry is None:
            raise self._unavailable("query")
        return [
            SnapshotSearchResult(title=item["title"], url=item["url"])
            for item in entry["results"][:limit]
        ]

    async def open(self, url: str, max_chars: int) -> dict[str, Any]:
        self.calls["open"] += 1
        entry = self._pages.get(url)
        if entry is None:
            raise self._unavailable("url")
        content = str(entry["content"])
        return {
            "url": url,
            "title": entry["title"],
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
            "redirect_count": 0,
            "external_untrusted": True,
            "snapshot_version": self.version,
        }

    async def validate(self, url: str) -> None:
        self.calls["validate"] += 1
        if url not in self._pages:
            raise self._unavailable("url")
