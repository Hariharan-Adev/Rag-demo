"""Semantic document-search endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.config import settings
from app.services.vector_search import search_chunks
from app.utils.audit import log_audit_event
from app.utils.rate_limit import enforce_request_limit
from app.utils.security import make_preview

router = APIRouter(prefix="/search", tags=["search"])


class SearchRequest(BaseModel):
    """Question submitted for document retrieval."""

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=3, ge=1, le=10)
    collection_id: int | None = Field(default=None, gt=0)
    document_id: int | None = Field(default=None, gt=0)
    version_id: int | None = Field(default=None, gt=0)


@router.post("")
def search_documents(
    api_request: Request,
    request: SearchRequest,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Retrieve the document chunks most relevant to a question."""
    client_ip = api_request.client.host if api_request.client else "unknown"

    try:
        enforce_request_limit(
            int(current_user["id"]),
            client_ip,
            "search",
            settings.search_requests_per_hour,
        )
    except HTTPException as error:
        if error.status_code == 429:
            log_audit_event(
                event_type="search.query",
                endpoint="search",
                outcome="rate_limited",
                user_id=int(current_user["id"]),
                client_ip=client_ip,
                metadata={"reason": "hourly_request_limit"},
            )
        raise

    query = request.query.strip()

    if not query:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    results = [
        {
            "chunk_id": result["chunk_id"],
            "document_id": result["document_id"],
            "version_id": result["version_id"],
            "filename": result["filename"],
            "referencing_filenames": result["referencing_filenames"],
            "sheet_name": result["sheet_name"],
            "row_number": result["row_number"],
            "source_type": result["source_type"],
            "source_location": result["source_location"],
            "location": {
                "source_type": result["source_type"],
                **result["source_location"],
            },
            "retrieval_score": result["score"],
            "text": result["content"],
            "preview": make_preview(str(result["content"])),
        }
        for result in search_chunks(
            query,
            int(current_user["id"]),
            request.limit,
            request.collection_id,
            request.document_id,
            version_id=request.version_id,
        )
    ]

    log_audit_event(
        event_type="search.query",
        endpoint="search",
        outcome="success" if results else "no_results",
        user_id=int(current_user["id"]),
        client_ip=client_ip,
        metadata={"result_count": len(results)},
    )

    return {
        "query": query,
        "result_count": len(results),
        "results": results,
    }
