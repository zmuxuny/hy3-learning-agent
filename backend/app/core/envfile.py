from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _default_env_path() -> Path:
    return PROJECT_ROOT / ".env"


@contextmanager
def _locked_parent(path: Path) -> Iterator[int]:
    """Serialize read-modify-write across processes without a stale lock file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        yield directory
    finally:
        fcntl.flock(directory, fcntl.LOCK_UN)
        os.close(directory)


def _render_update(existing_bytes: bytes, values: dict[str, str]) -> bytes:
    existing = existing_bytes.decode("utf-8").splitlines()
    output: list[str] = []
    written: set[str] = set()
    for line in existing:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in values:
                output.append(f"{key}={values[key]}")
                written.add(key)
                continue
        output.append(line)
    for key, value in values.items():
        if key not in written:
            output.append(f"{key}={value}")
    return ("\n".join(output) + "\n").encode("utf-8")


def _atomic_replace(path: Path, payload: bytes, directory: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".env-tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.fsync(directory)
    finally:
        temporary.unlink(missing_ok=True)


def update_env_file(values: dict[str, str], env_path: Path | None = None) -> None:
    """Durably update a local .env under one cross-process RMW lock."""

    path = env_path or _default_env_path()
    with _locked_parent(path) as directory:
        existing = path.read_bytes() if path.exists() else b""
        _atomic_replace(path, _render_update(existing, values), directory)


def clear_env_keys(keys: list[str], env_path: Path | None = None) -> None:
    update_env_file({key: "" for key in keys}, env_path)
