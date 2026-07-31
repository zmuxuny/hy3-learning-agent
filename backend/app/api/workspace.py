from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.config import PROJECT_ROOT


router = APIRouter()
WORKSPACE_ROOT = (PROJECT_ROOT / "data" / "workspace").resolve()
UPLOAD_ROOT = WORKSPACE_ROOT / "uploads"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@router.post("/files", status_code=201)
async def upload_workspace_file(file: UploadFile = File(...)):
    original = Path(file.filename or "artifact").name
    suffix = Path(original).suffix[:16]
    stem = Path(original).stem[:80] or "artifact"
    target = UPLOAD_ROOT / f"{stem}-{uuid4().hex[:8]}{suffix}"
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 5 MB upload limit")
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {
        "path": str(target.relative_to(WORKSPACE_ROOT)),
        "name": original,
        "size": len(content),
        "content_type": file.content_type or "application/octet-stream",
    }
