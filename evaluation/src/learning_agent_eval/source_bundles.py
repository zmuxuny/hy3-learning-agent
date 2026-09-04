"""Deterministic conservative source bundles for active evaluation components."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from .canonical import sha256_digest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_BUNDLE_VERSION = "evaluation-source-bundle-v2"
SourceComponent = Literal["runtime", "rules", "judge", "aggregate"]
SOURCE_COMPONENTS: tuple[SourceComponent, ...] = (
    "aggregate",
    "judge",
    "rules",
    "runtime",
)


def source_bundle_paths(
    component: SourceComponent, *, project_root: Path = PROJECT_ROOT
) -> tuple[str, ...]:
    """Return the documented conservative source superset for a component."""

    evaluation_root = project_root / "evaluation" / "src" / "learning_agent_eval"
    paths = {
        path.relative_to(project_root).as_posix()
        for path in evaluation_root.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    if component == "runtime":
        backend_root = project_root / "backend" / "app"
        paths.update(
            path.relative_to(project_root).as_posix()
            for path in backend_root.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    if component not in SOURCE_COMPONENTS:
        raise ValueError("unknown source bundle component")
    if not paths:
        raise ValueError("source bundle cannot be empty")
    return tuple(sorted(paths))


def build_source_bundle(
    component: SourceComponent, *, project_root: Path = PROJECT_ROOT
) -> dict[str, object]:
    """Build a path-independent manifest over exact source bytes."""

    files: list[dict[str, object]] = []
    for relative in source_bundle_paths(component, project_root=project_root):
        payload = (project_root / relative).read_bytes()
        files.append(
            {
                "relative_path": relative,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    document: dict[str, object] = {
        "schema_version": "source-bundle-manifest-v1",
        "bundle_version": SOURCE_BUNDLE_VERSION,
        "component": component,
        "inclusion_policy": (
            "evaluation-and-product-runtime-conservative-v1"
            if component == "runtime"
            else "evaluation-package-conservative-v1"
        ),
        "files": files,
        "bundle_sha256": "0" * 64,
    }
    document["bundle_sha256"] = sha256_digest(
        {key: value for key, value in document.items() if key != "bundle_sha256"}
    )
    return document


def source_bundle_sha256(
    component: SourceComponent, *, project_root: Path = PROJECT_ROOT
) -> str:
    """Return the current bundle digest for one active component."""

    return str(build_source_bundle(component, project_root=project_root)["bundle_sha256"])
