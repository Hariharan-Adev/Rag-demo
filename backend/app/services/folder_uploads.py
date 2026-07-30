"""Tenant-and-owner-scoped collection and upload-batch helpers."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from fastapi import HTTPException

from app.database import get_connection
from app.utils.audit import log_audit_event

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_UNSAFE_PATH_CHARACTER = re.compile(r"[^\w .()\-]+", re.UNICODE)


def sanitize_relative_path(value: object | None, filename: str) -> str | None:
    """Validate and normalize a browser-relative path for metadata storage only."""
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    raw = value.strip()
    if "\\" in raw or _CONTROL_CHARACTERS.search(raw) or raw.startswith("/") or _DRIVE_PREFIX.match(raw):
        raise HTTPException(status_code=400, detail="The relative path is not valid.")
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="The relative path is not valid.")
    sanitized = [(_UNSAFE_PATH_CHARACTER.sub("_", part).strip(" .") or "folder") for part in parts]
    sanitized[-1] = filename
    return "/".join(sanitized)


def validate_upload_context(owner_id: int, collection_id: int | None, batch_id: int | None) -> None:
    if collection_id is None and batch_id is None:
        return
    started = False
    organization_id: str | None = None
    with get_connection() as connection:
        user = connection.execute(
            "SELECT organization_id FROM users WHERE id = ? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
        if user is None:
            raise HTTPException(status_code=401, detail="User was not found.")
        organization_id = str(user["organization_id"])
        collection = connection.execute(
            """SELECT id FROM document_collections
               WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (collection_id, organization_id, owner_id),
        ).fetchone() if collection_id is not None else None
        if collection_id is not None and collection is None:
            raise HTTPException(status_code=404, detail="Collection was not found.")
        if batch_id is not None:
            batch = connection.execute(
                """SELECT collection_id, status, processed_files, total_files
                   FROM upload_batches
                   WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                (batch_id, organization_id, owner_id),
            ).fetchone()
            if batch is None:
                raise HTTPException(status_code=404, detail="Upload batch was not found.")
            if collection_id is None or int(batch["collection_id"]) != collection_id:
                raise HTTPException(status_code=400, detail="Upload batch does not belong to the selected collection.")
            if batch["status"] == "cancelled":
                raise HTTPException(status_code=409, detail="Upload batch was cancelled.")
            if int(batch["processed_files"]) >= int(batch["total_files"]):
                raise HTTPException(status_code=409, detail="Upload batch is already complete.")
            connection.execute(
                """UPDATE upload_batches SET status = 'uploading'
                   WHERE id = ? AND organization_id = ? AND owner_id = ?
                     AND status = 'created'""",
                (batch_id, organization_id, owner_id),
            )
            started = batch["status"] == "created"
    if started:
        log_audit_event(event_type="folder.upload_started", endpoint="documents/upload", outcome="success", user_id=owner_id, organization_id=organization_id, client_ip="", metadata={"batch_id": batch_id})


def record_batch_result(batch_id: int | None, owner_id: int, outcome: str) -> None:
    if batch_id is None:
        return
    counter = {
        "successful": "successful_files",
        "duplicate": "duplicate_files",
        "skipped": "skipped_files",
        "failed": "failed_files",
    }.get(outcome)
    if counter is None:
        raise ValueError("Unsupported batch outcome.")
    completed_status: str | None = None
    organization_id: str | None = None
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute(
            "SELECT organization_id FROM users WHERE id = ? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
        if user is None:
            return
        organization_id = str(user["organization_id"])
        row = connection.execute(
            """SELECT * FROM upload_batches
               WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (batch_id, organization_id, owner_id),
        ).fetchone()
        if row is None or row["status"] == "cancelled":
            return
        connection.execute(
            f"""UPDATE upload_batches
                SET processed_files = processed_files + 1,
                    {counter} = {counter} + 1
                WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (batch_id, organization_id, owner_id),
        )
        updated = connection.execute(
            """SELECT * FROM upload_batches
               WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (batch_id, organization_id, owner_id),
        ).fetchone()
        if int(updated["processed_files"]) >= int(updated["total_files"]):
            completed_status = "failed" if int(updated["failed_files"]) == int(updated["total_files"]) else (
                "partially_completed" if int(updated["failed_files"]) or int(updated["skipped_files"]) else "completed"
            )
            connection.execute(
                """UPDATE upload_batches
                   SET status = ?, completed_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                (completed_status, batch_id, organization_id, owner_id),
            )
    if completed_status is not None:
        log_audit_event(event_type="folder.batch_completed", endpoint="documents/upload", outcome=completed_status, user_id=owner_id, organization_id=organization_id, client_ip="", metadata={"batch_id": batch_id})
