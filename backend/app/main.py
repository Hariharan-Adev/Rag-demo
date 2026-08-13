"""FastAPI application entry point."""

from threading import Event, Lock, Thread
from fastapi import FastAPI
from uuid import uuid4
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import initialize_database
from app.routes.auth import router as auth_router
from app.routes.chat import router as chat_router
from app.routes.collections import router as collections_router
from app.routes.documents import router as documents_router
from app.routes.images import router as images_router
from app.routes.health import router as health_router
from app.routes.ingestion import public_router as public_ingestion_router
from app.routes.ingestion import router as ingestion_router
from app.routes.search import router as search_router
from app.routes.upload import router as upload_router
from app.utils.observability import log_event
from app.worker import run_worker

_worker_stop = Event()
_worker_lock = Lock()
_worker_thread: Thread | None = None


def _start_ingestion_worker() -> None:
    """Start one API-owned ingestion worker for this application process."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_stop.clear()
        _worker_thread = Thread(
            target=run_worker,
            args=(_worker_stop,),
            name="ingestion-worker",
            daemon=True,
        )
        _worker_thread.start()


def _stop_ingestion_worker() -> None:
    """Signal the API-owned ingestion worker during application shutdown."""
    _worker_stop.set()
    thread = _worker_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=max(2.0, settings.ingestion_poll_seconds + 1.0))

app = FastAPI(title="Simple RAG API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://docsenseai.recezy.ai",
        "http://192.168.1.235"
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(public_ingestion_router)
app.include_router(upload_router)
app.include_router(documents_router)
app.include_router(images_router)
app.include_router(ingestion_router)
app.include_router(health_router)
app.include_router(search_router)
app.include_router(chat_router)
app.include_router(collections_router)


@app.on_event("startup")
def startup() -> None:
    """Initialize persistence and start ingestion with the API."""
    initialize_database()
    _start_ingestion_worker()


@app.on_event("shutdown")
def shutdown() -> None:
    """Stop the API-owned ingestion worker."""
    _stop_ingestion_worker()


@app.middleware("http")
async def request_observability(request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    log_event(
        "http.request",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
    )
    return response
