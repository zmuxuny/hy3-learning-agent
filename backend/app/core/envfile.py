from __future__ import annotations

import os
from pathlib import Path

from app.core.config import PROJECT_ROOT


def _default_env_path() -> Path:
    return PROJECT_ROOT / ".env"


def update_env_file(values: dict[str, str], env_path: Path | None = None) -> None:
    """Atomically update KEY=VALUE lines in the local .env file (0600 perms)."""
    path = env_path or _default_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
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
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def clear_env_keys(keys: list[str], env_path: Path | None = None) -> None:
    update_env_file({key: "" for key in keys}, env_path)
