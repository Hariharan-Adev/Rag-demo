"""Standalone durable ingestion worker; run separately from the FastAPI process."""

from __future__ import annotations

import argparse
import logging
import socket
from threading import Event
from time import sleep
from uuid import uuid4

from app.config import settings
from app.database import initialize_database
from app.services.embeddings import get_model
from app.services.ingestion_jobs import run_one

logger = logging.getLogger(__name__)


def run_worker(stop_event: Event | None = None) -> None:
    """Process ingestion jobs until shutdown is requested."""
    initialize_database()
    try:
        # Model imports are expensive on a cold Windows process. Load them before
        # claiming a job so an upload is never left marked as processing while
        # the embedding runtime initializes.
        get_model()
    except Exception:
        logger.exception("Embedding model warm-up failed; jobs will report the failure.")
    worker_id = f"{socket.gethostname()}:{uuid4()}"
    while stop_event is None or not stop_event.is_set():
        processed = run_one(worker_id)
        if not processed:
            if stop_event is None:
                sleep(settings.ingestion_poll_seconds)
            else:
                stop_event.wait(settings.ingestion_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run document ingestion jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one job.")
    arguments = parser.parse_args()
    if arguments.once:
        initialize_database()
        run_one(f"{socket.gethostname()}:{uuid4()}")
        return
    run_worker()


if __name__ == "__main__":
    main()
