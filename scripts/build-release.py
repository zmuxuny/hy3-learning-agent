#!/usr/bin/env python3
"""Build a deterministic Learning Agent runtime release archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path


ROOT_FILES = (".env.example", "CHANGELOG.md", "LICENSE", "README.md")
RUNTIME_TREES = ("assets", "backend", "docs")
RUNTIME_SCRIPTS = (
    "data-maintenance.py",
    "demo-data.sh",
    "demo-preflight.sh",
    "evidence-baseline.py",
    "rebuild-evidence.py",
    "reset-data.sh",
    "seed-fixture.sh",
    "setup.sh",
    "start.sh",
)
EXCLUDED_PARTS = {
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "data",
    "logs",
    "node_modules",
}
EXCLUDED_SUFFIXES = (".db", ".db-shm", ".db-wal", ".log", ".pyc", ".sqlite3")
EXCLUDED_PATHS = {"backend/requirements-dev.txt"}


class ReleaseBuildError(RuntimeError):
    pass


def _safe_source(path: Path) -> Path:
    if path.is_symlink():
        raise ReleaseBuildError("invalid_release_source")
    source = path.resolve(strict=True)
    if not source.is_dir():
        raise ReleaseBuildError("invalid_release_source")
    return source


def _is_excluded(relative: Path) -> bool:
    if relative.as_posix() in EXCLUDED_PATHS:
        return True
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return True
    if relative.name in {".env", ".env.local"}:
        return True
    return relative.name.endswith(EXCLUDED_SUFFIXES)


def _collect_tree(source: Path, relative_root: Path) -> dict[str, bytes]:
    root = source / relative_root
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ReleaseBuildError(f"unsafe_release_input:{relative_root.as_posix()}")
    payload: dict[str, bytes] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in [*names, *files]:
            candidate = current / name
            relative = candidate.relative_to(source)
            if candidate.is_symlink():
                raise ReleaseBuildError(f"unsafe_release_symlink:{relative.as_posix()}")
        names[:] = sorted(name for name in names if not _is_excluded((current / name).relative_to(source)))
        for name in sorted(files):
            candidate = current / name
            relative = candidate.relative_to(source)
            if _is_excluded(relative):
                continue
            if not candidate.is_file():
                raise ReleaseBuildError(f"unsafe_release_input:{relative.as_posix()}")
            payload[relative.as_posix()] = candidate.read_bytes()
    return payload


def _collect_payload(source: Path) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    for name in ROOT_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ReleaseBuildError(f"missing_release_input:{name}")
        payload[name] = path.read_bytes()
    for tree in RUNTIME_TREES:
        payload.update(_collect_tree(source, Path(tree)))
    for name in RUNTIME_SCRIPTS:
        path = source / "scripts" / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ReleaseBuildError(f"unsafe_release_input:scripts/{name}")
            payload[f"scripts/{name}"] = path.read_bytes()
    payload.update(_collect_tree(source, Path("frontend/dist")))
    if "frontend/dist/index.html" not in payload:
        raise ReleaseBuildError("missing_release_asset")
    if "scripts/start.sh" not in payload or "backend/run.py" not in payload:
        raise ReleaseBuildError("missing_release_runtime")
    return payload


def _mode_for(path: str, data: bytes) -> int:
    if path.startswith("scripts/") and (path.endswith(".sh") or data.startswith(b"#!")):
        return 0o755
    return 0o644


def _manifest(payload: dict[str, bytes], release_name: str) -> bytes:
    document = {
        "format_version": 1,
        "release": release_name,
        "files": [
            {
                "path": path,
                "size": len(payload[path]),
                "sha256": hashlib.sha256(payload[path]).hexdigest(),
            }
            for path in sorted(payload)
        ],
    }
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def build_release(source: Path, output: Path) -> tuple[Path, str]:
    source = _safe_source(source)
    if output.is_symlink():
        raise ReleaseBuildError("unsafe_release_output")
    output = output.resolve()
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise ReleaseBuildError("release_output_must_be_outside_source")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ReleaseBuildError("unsafe_release_output")
    checksum_path = output.with_name(f"{output.name}.sha256")
    if checksum_path.is_symlink() or (checksum_path.exists() and not checksum_path.is_file()):
        raise ReleaseBuildError("unsafe_release_checksum_output")
    output.parent.mkdir(parents=True, exist_ok=True)

    payload = _collect_payload(source)
    version_source = payload.get("backend/app/version.py", b"").decode("utf-8", errors="replace")
    version = "unknown"
    for line in version_source.splitlines():
        if line.startswith("APPLICATION_VERSION") and '"' in line:
            version = line.split('"', 2)[1]
            break
    release_name = f"learning-agent-{version}"
    payload["RELEASE-MANIFEST.json"] = _manifest(payload, release_name)

    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("wb", buffering=0) as raw:
            with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
                directories = {release_name}
                for relative in payload:
                    parts = Path(relative).parts
                    for index in range(1, len(parts)):
                        directories.add(f"{release_name}/{'/'.join(parts[:index])}")
                for directory in sorted(directories):
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info)
                for relative, data in sorted(payload.items()):
                    info = tarfile.TarInfo(f"{release_name}/{relative}")
                    info.size = len(data)
                    info.mode = _mode_for(relative, data)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))
            os.fsync(raw.fileno())
        temporary.replace(output)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        checksum_path.write_text(f"{digest}  {output.name}\n", encoding="ascii")
        return output, digest
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        output, digest = build_release(arguments.source, arguments.output)
    except (OSError, ReleaseBuildError) as exc:
        print(str(exc), file=os.sys.stderr)
        return 2
    print(json.dumps({"ok": True, "archive": str(output), "sha256": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
