"""Create local vector embeddings for RAG retrieval."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
model: Any | None = None


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
    """Load the embedding model once and reuse it."""
    global model

    if model is None:
        from sentence_transformers import SentenceTransformer

        cached_path = _cached_model_path()
        # Avoid an unbounded Hugging Face network lookup on every cold worker.
        # If no complete local snapshot exists, SentenceTransformer can still
        # download it normally on the first run.
        model = SentenceTransformer(
            str(cached_path) if cached_path is not None else MODEL_NAME,
            local_files_only=cached_path is not None,
        )

    return model


def create_embeddings(texts: list[str]) -> list[list[float]]:
    """Convert text chunks into normalized vector embeddings."""
    embedding_model = get_model()

    return embedding_model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
    ).tolist()
