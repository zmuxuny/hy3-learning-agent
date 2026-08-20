from __future__ import annotations

import fcntl
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UNQUOTED_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_./:@%+,=-]*$")


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


def _contains_forbidden_control(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or character in {"\u2028", "\u2029"}
        for character in value
    )


def _validated_updates(values: dict[str, str]) -> dict[str, str]:
    if not isinstance(values, dict):
        raise ValueError(".env updates must be a string mapping")
    validated: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(".env key is not a valid identifier")
        if not isinstance(value, str):
            raise ValueError(".env value must be a string")
        if _contains_forbidden_control(value):
            raise ValueError(".env value contains a forbidden control character")
        validated[key] = value
    return validated


def _quote_env_value(value: str) -> str:
    if value and _UNQUOTED_VALUE_PATTERN.fullmatch(value):
        return value
    # python-dotenv expands ${NAME} even inside quotes. Split the dollar from
    # the opening brace through an empty-name default expression; interpolation
    # is single-pass, so loading produces the exact original literal.
    escaped = value.replace("${", "${:-$}{")
    escaped = escaped.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _assignment_key(line: str) -> str | None:
    candidate = line.lstrip()
    if candidate.startswith("export "):
        candidate = candidate[len("export ") :].lstrip()
    if "=" not in candidate or candidate.startswith("#"):
        return None
    key = candidate.split("=", 1)[0].strip()
    return key if _ENV_KEY_PATTERN.fullmatch(key) else None


def _render_update(existing_bytes: bytes, values: dict[str, str]) -> bytes:
    existing = existing_bytes.decode("utf-8").splitlines()
    output: list[str] = []
    written: set[str] = set()
    for line in existing:
        key = _assignment_key(line)
        if key in values:
            output.append(f"{key}={_quote_env_value(values[key])}")
            written.add(key)
            continue
        output.append(line)
    for key, value in values.items():
        if key not in written:
            output.append(f"{key}={_quote_env_value(value)}")
    return ("\n".join(output) + "\n").encode("utf-8")


def _read_regular_file(path: Path) -> bytes:
    # O_NONBLOCK prevents an attacker-controlled FIFO from stalling startup;
    # fstat still rejects every non-regular descriptor before any read.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise ValueError(".env target must be a regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(".env target must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


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

    validated = _validated_updates(values)
    path = env_path or _default_env_path()
    with _locked_parent(path) as directory:
        existing = _read_regular_file(path)
        _atomic_replace(path, _render_update(existing, validated), directory)


def clear_env_keys(keys: list[str], env_path: Path | None = None) -> None:
    update_env_file({key: "" for key in keys}, env_path)
