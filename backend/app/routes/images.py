"""Authenticated screenshot upload routed through the shared RAG ingestion pipeline."""

from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.auth import get_current_user
from app.routes.upload import upload_document
from app.services.image_processor import IMAGE_EXTENSIONS
from app.utils.document_content import sanitize_filename

router = APIRouter(prefix="/api", tags=["images"])


@router.post("/upload-image")
async def upload_image(
    request: Request,
    image: UploadFile = File(...),
    current_user: dict[str, object] = Depends(get_current_user),
    document_type: str = Form(default="screenshot"),
    collection_id: int | None = Form(default=None),
    upload_batch_id: int | None = Form(default=None),
    relative_path: str | None = Form(default=None),
):
    """Validate an image, then reuse normal deduplication, chunking, and indexing."""
    try:
        safe_filename = sanitize_filename(image.filename or "", set(IMAGE_EXTENSIONS))
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail="Only supported screenshot and image formats may use this endpoint.",
        ) from error

    normalized_type = document_type.strip().lower()
    if normalized_type not in {"screenshot", "image"}:
        raise HTTPException(
            status_code=400,
            detail="document_type must be either 'screenshot' or 'image'.",
        )

    result = await upload_document(
        request=request,
        file=image,
        current_user=current_user,
        collection_id=collection_id,
        upload_batch_id=upload_batch_id,
        relative_path=relative_path,
    )
    if isinstance(result, JSONResponse):
        return result

    detected_type = image.content_type or guess_type(safe_filename)[0]
    if not detected_type or not detected_type.startswith("image/"):
        detected_type = f"image/{Path(safe_filename).suffix.lower().lstrip('.')}"
    result.update(
        {
            "file_type": detected_type,
            "document_type": normalized_type,
            "extraction": "ocr+vision-with-ocr-fallback",
        }
    )
    return result
