"""Create local vector embeddings for RAG retrieval."""

from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, Any

from app.config import settings
from app.utils.observability import log_event

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
model: Any | None = None
_model_lock = Lock()
_model_status = "uninitialized"
_model_error_type: str | None = None


def _cached_model_path() -> Path | None:
    """Resolve a complete Hugging Face snapshot without making a network request."""
    model_cache = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / f"models--{MODEL_NAME.replace('/', '--')}"
    )
    revision_file = model_cache / "refs" / "main"
    if not revision_file.is_file():
        return None
    revision = revision_file.read_text(encoding="utf-8").strip()
    snapshot = model_cache / "snapshots" / revision
    required_files = ("config.json", "model.safetensors", "tokenizer.json")
    return snapshot if all((snapshot / name).is_file() for name in required_files) else None


def get_model() -> "SentenceTransformer":
    """Load one local model and bound concurrent callers waiting for it."""
    global model, _model_error_type, _model_status

    if model is not None:
        return model
    if not _model_lock.acquire(timeout=settings.embedding_model_load_timeout_seconds):
        log_event("embedding.model.wait_timeout")
        raise RuntimeError("Embedding model initialization is still in progress.")
    try:
        if model is not None:
            return model

        _model_status = "loading"
        _model_error_type = None
        started = perf_counter()
        try:
            from sentence_transformers import SentenceTransformer

            cached_path = _cached_model_path()
            # Use a complete local snapshot without contacting Hugging Face.
            # First-time setup still permits the existing download behavior.
            model = SentenceTransformer(
                str(cached_path) if cached_path is not None else MODEL_NAME,
                local_files_only=cached_path is not None,
            )
        except Exception as error:
            _model_status = "failed"
            _model_error_type = type(error).__name__
            log_event(
                "embedding.model.load_failed",
                duration_ms=round((perf_counter() - started) * 1000),
                error_type=_model_error_type,
            )
            raise RuntimeError("Local embedding model could not be initialized.") from error

        _model_status = "ready"
        log_event(
            "embedding.model.loaded",
            duration_ms=round((perf_counter() - started) * 1000),
            cached=bool(cached_path),
        )
        return model
    finally:
        _model_lock.release()


def embedding_health() -> dict[str, object]:
    """Report safe embedding state without forcing model initialization."""
    return {
        "status": _model_status,
        "loaded": model is not None,
        "error_type": _model_error_type,
    }


def reset_model_for_tests() -> None:
    """Reset the process-local model state for isolated unit tests."""
    global model, _model_error_type, _model_status
    with _model_lock:
        model = None
        _model_status = "uninitialized"
        _model_error_type = None


def create_embeddings(texts: list[str]) -> list[list[float]]:
    """Convert text chunks into normalized vector embeddings."""
    embedding_model = get_model()

    return embedding_model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
    ).tolist()
