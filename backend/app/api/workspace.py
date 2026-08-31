import asyncio
import hashlib
import os
import tempfile
import unicodedata
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.config import settings


router = APIRouter()
WORKSPACE_ROOT = (settings.RUNTIME_STATE_ROOT / "data" / "workspace").resolve()
UPLOAD_ROOT = WORKSPACE_ROOT / "uploads"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _canonical_upload_name(filename: str | None) -> str:
    """Return one bounded, platform-independent identity for an upload name."""

    raw = unicodedata.normalize("NFKC", filename or "artifact").replace("\\", "/")
    basename = raw.rsplit("/", 1)[-1].strip()
    safe = "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "-"
        for character in basename
    )
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip(".-") or "artifact"
    suffix = Path(safe).suffix[:16]
    stem = Path(safe).stem.strip(".-")[:80] or "artifact"
    return f"{stem}{suffix}"


def _upload_digest(name: str, content: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"learning-travel-upload-v1\0")
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(content)
    return digest.hexdigest()


def _atomic_publish_upload(target: Path, content: bytes) -> None:
    """Durably publish complete bytes; concurrent writers use unique staging files."""

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".upload-tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(
            target.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@router.post("/files", status_code=201)
async def upload_workspace_file(file: UploadFile = File(...)):
    canonical_name = _canonical_upload_name(file.filename)
    suffix = Path(canonical_name).suffix
    stem = Path(canonical_name).stem
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    content_type = file.content_type or "application/octet-stream"
    await file.close()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 5 MB upload limit")
    request_digest = _upload_digest(canonical_name, content)
    target = UPLOAD_ROOT / f"{stem}-{request_digest}{suffix}"
    await asyncio.to_thread(_atomic_publish_upload, target, content)
    return {
        "path": str(target.relative_to(WORKSPACE_ROOT)),
        "name": canonical_name,
        "size": len(content),
        "content_type": content_type,
    }
