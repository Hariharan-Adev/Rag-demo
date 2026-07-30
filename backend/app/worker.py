"""Standalone durable ingestion worker; run separately from the FastAPI process."""

from __future__ import annotations

import argparse
import socket
from time import sleep
from uuid import uuid4

from app.config import settings
from app.database import initialize_database
from app.services.ingestion_jobs import run_one


def main() -> None:
    parser = argparse.ArgumentParser(description="Run document ingestion jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one job.")
    arguments = parser.parse_args()
    initialize_database()
    worker_id = f"{socket.gethostname()}:{uuid4()}"
    while True:
        processed = run_one(worker_id)
        if arguments.once:
            return
        if not processed:
            sleep(settings.ingestion_poll_seconds)


if __name__ == "__main__":
    main()
