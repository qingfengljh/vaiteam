"""通用文件上传 API — 项目级，不绑定 Stage"""

import uuid
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models import Project, UploadedFile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects", tags=["uploads"])

UPLOAD_DIR = Path("/tmp/openclaw-uploads")
MAX_FILE_SIZE = 50 * 1024 * 1024


@router.post("/{project_id}/upload")
async def upload_file(
    project_id: str,
    file: UploadFile = File(...),
    hint: str = Form(""),
    uploader: str = Form("human"),
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(400, f"文件大小不能超过 {MAX_FILE_SIZE // 1024 // 1024}MB")

    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()

    upload_dir = UPLOAD_DIR / project_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
    stored_path = str(upload_dir / safe_name)
    (upload_dir / safe_name).write_bytes(data)

    from app.services.doc_parser import parse_document, IMAGE_FORMATS
    is_image = ext in IMAGE_FORMATS
    result = await parse_document(data, filename, vision_analyze=is_image)

    record = UploadedFile(
        project_id=project_id,
        uploader=uploader,
        original_name=filename,
        stored_path=stored_path,
        format=ext,
        size=len(data),
        is_image=is_image,
        description=result.text,
        metadata_=result.metadata,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    return {
        "id": record.id,
        "original_name": record.original_name,
        "format": record.format,
        "size": record.size,
        "is_image": record.is_image,
        "description": record.description,
        "created_at": record.created_at.isoformat(),
    }


@router.get("/{project_id}/uploads")
async def list_uploads(
    project_id: str,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    q = (
        select(UploadedFile)
        .where(UploadedFile.project_id == project_id)
        .order_by(UploadedFile.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(q)
    return [
        {
            "id": f.id,
            "original_name": f.original_name,
            "format": f.format,
            "size": f.size,
            "is_image": f.is_image,
            "description": f.description[:200] if f.description else "",
            "created_at": f.created_at.isoformat(),
        }
        for f in result.scalars()
    ]
