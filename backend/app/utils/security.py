"""Security checks for uploaded documents and model output."""

import re

MAX_EXTRACTED_TEXT_CHARS = 200_000
MAX_CHUNKS_PER_DOCUMENT = 500
PREVIEW_CHARS = 240

PROMPT_INJECTION_PATTERNS = [
    re.compile(
        r"\b(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+"
        r"(instructions|context|rules|messages)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(show|reveal|print|repeat|display|output|tell me)\s+"
        r"(your|the)\s+(system prompt|developer message|instructions)\b",
        re.IGNORECASE,
    ),
    re.compile(r"###\s*(system|instruction|override)\s*###", re.IGNORECASE),
    re.compile(r"\[(system|developer)\s*(override|prompt|message)\]", re.IGNORECASE),
    re.compile(r"\b(developer mode|jailbreak|DAN mode|do anything now)\b", re.IGNORECASE),
    re.compile(
        r"\b(bypass|circumvent|disable)\s+(safety|guardrails|filters|policy|approval)\b",
        re.IGNORECASE,
    ),
]

SYSTEM_DISCLOSURE_PATTERNS = [
    re.compile(r"\b(system prompt|developer message|hidden instructions)\b", re.IGNORECASE),
    re.compile(r"\bmy instructions are\b", re.IGNORECASE),
    re.compile(r"\bI was instructed to\b", re.IGNORECASE),
]


class SecurityValidationError(ValueError):
    """Raised when uploaded content fails a security gate."""


def validate_extracted_text(text: str) -> None:
    """Reject empty or oversized text while treating content as untrusted data."""
    if not text.strip():
        raise SecurityValidationError("The document contains no readable text.")

    if len(text) > MAX_EXTRACTED_TEXT_CHARS:
        raise SecurityValidationError(
            f"Extracted text exceeds {MAX_EXTRACTED_TEXT_CHARS} characters."
        )


def validate_chunks(chunks: list[str]) -> None:
    """Reject documents that would create too many retrieval chunks."""
    if not chunks:
        raise SecurityValidationError("The document contains no readable text.")

    if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
        raise SecurityValidationError(
            f"The document creates more than {MAX_CHUNKS_PER_DOCUMENT} chunks."
        )


def make_preview(text: str, max_chars: int = PREVIEW_CHARS) -> str:
    """Return a compact preview without exposing the full retrieval chunk."""
    normalized = " ".join(text.split())

    if len(normalized) <= max_chars:
        return normalized

    return f"{normalized[: max_chars - 3]}..."


def guard_model_output(text: str) -> str:
    """Block likely system-prompt disclosure from model output."""
    for pattern in SYSTEM_DISCLOSURE_PATTERNS:
        if pattern.search(text):
            return "The answer was blocked because it may disclose hidden instructions."

    return text
