"""RAG chat endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.config import settings
from app.database import get_connection
from app.services.chat_history import (
    append_exchange,
    delete_conversation,
    list_conversations,
    update_conversation,
    upsert_conversation,
)
from app.services.document_access import require_document
from app.services.rag_diagnostics import (
    RagRequestDiagnostic,
    authorized_diagnostic_payload,
)
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


class ConversationCreateRequest(BaseModel):
    """Client-created conversation shell for persisted history."""

    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=48)


class ConversationUpdateRequest(BaseModel):
    """Editable conversation metadata."""

    title: str | None = Field(default=None, min_length=1, max_length=48)
    is_pinned: bool | None = None


@router.get("/conversations")
def conversations(
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Return backend-persisted chat history for the logged-in user."""
    return {"conversations": list_conversations(int(current_user["id"]))}


@router.post("/conversations", status_code=201)
def create_conversation(
    request: ConversationCreateRequest,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Persist a new conversation before the first message is sent."""
    upsert_conversation(int(current_user["id"]), request.id, request.title)
    return {"id": request.id}


@router.patch("/conversations/{conversation_id}")
def patch_conversation(
    conversation_id: str,
    request: ConversationUpdateRequest,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Rename or pin one owner-scoped conversation."""
    updated = update_conversation(
        owner_id=int(current_user["id"]),
        conversation_id=conversation_id,
        title=request.title,
        is_pinned=request.is_pinned,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"id": conversation_id, "updated": True}


@router.delete("/conversations/{conversation_id}")
def remove_conversation(
    conversation_id: str,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Soft-delete one owner-scoped conversation and its messages."""
    deleted = delete_conversation(int(current_user["id"]), conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"id": conversation_id, "deleted": True}


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
        result = answer_question(
            request.question.strip(),
            int(current_user["id"]),
            client_ip,
            request.collection_id,
            request.document_id,
            request.version_id,
            request.conversation_id,
        )
        if request.conversation_id:
            append_exchange(
                owner_id=int(current_user["id"]),
                conversation_id=request.conversation_id,
                question=request.question.strip(),
                answer=str(result.get("answer") or ""),
                sources=[
                    source for source in (result.get("sources") or [])
                    if isinstance(source, dict)
                ],
            )
        return result
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="The AI answer service is unavailable.",
        ) from error


@router.post("/diagnostics", tags=["development diagnostics"])
def diagnose_chat(
    api_request: Request,
    request: ChatRequest,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Run one authenticated, ACL-filtered RAG request without returning content."""
    if not settings.rag_diagnostics_enabled:
        # A disabled development capability is deliberately indistinguishable
        # from an endpoint that is not installed.
        raise HTTPException(status_code=404, detail="Not found.")
    client_ip = api_request.client.host if api_request.client else "unknown"
    enforce_request_limit(
        int(current_user["id"]),
        client_ip,
        "chat",
        settings.chat_requests_per_hour,
    )
    if request.document_id is not None:
        with get_connection() as connection:
            require_document(connection, request.document_id, current_user)
    diagnostic = RagRequestDiagnostic()
    try:
        answer_question(
            request.question.strip(),
            int(current_user["id"]),
            client_ip=client_ip,
            collection_id=request.collection_id,
            document_id=request.document_id,
            version_id=request.version_id,
            conversation_id=request.conversation_id,
            diagnostic=diagnostic,
            persist_context=False,
        )
        return authorized_diagnostic_payload(diagnostic, current_user)
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(
            status_code=500,
            detail="The RAG diagnostic request failed.",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="The RAG diagnostic service is unavailable.",
        ) from error
