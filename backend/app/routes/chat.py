"""RAG chat endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.config import settings
from app.services.rag_service import answer_question
from app.utils.rate_limit import enforce_request_limit

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    """Question sent to the RAG assistant."""

    question: str = Field(min_length=1, max_length=1000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    collection_id: int | None = Field(default=None, gt=0)
    document_id: int | None = Field(default=None, gt=0)
    version_id: int | None = Field(default=None, gt=0)


@router.post("")
def chat(
    api_request: Request,
    request: ChatRequest,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Answer a question from uploaded documents."""
    client_ip = api_request.client.host if api_request.client else "unknown"

    enforce_request_limit(
        int(current_user["id"]),
        client_ip,
        "chat",
        settings.chat_requests_per_hour,
    )

    try:
        return answer_question(
            request.question.strip(),
            int(current_user["id"]),
            client_ip,
            request.collection_id,
            request.document_id,
            request.version_id,
            request.conversation_id,
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="The AI answer service is unavailable.",
        ) from error
