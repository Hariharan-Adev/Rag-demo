"""Minimal structured JSON logging with request/job correlation identifiers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("rag")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


def log_event(event: str, **fields: object) -> None:
    """Emit non-sensitive machine-readable operational telemetry."""
    logger.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }, default=str, separators=(",", ":")))
