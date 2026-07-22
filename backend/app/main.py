"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import initialize_database
from app.routes.auth import router as auth_router
from app.routes.chat import router as chat_router
from app.routes.collections import router as collections_router
from app.routes.documents import router as documents_router
from app.routes.search import router as search_router
from app.routes.upload import router as upload_router

app = FastAPI(title="Simple RAG API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(auth_router)
app.include_router(upload_router)
app.include_router(documents_router)
app.include_router(search_router)
app.include_router(chat_router)
app.include_router(collections_router)


@app.on_event("startup")
def startup() -> None:
    """Create the SQLite tables when the server starts."""
    initialize_database()


@app.get("/health")
def health_check() -> dict[str, str]:
    """Confirm that the API is running."""
    return {"status": "ok"}
