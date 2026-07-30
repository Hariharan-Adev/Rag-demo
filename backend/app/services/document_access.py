"""Centralized tenant and document access-control predicates."""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from fastapi import HTTPException
from app.utils.observability import log_event


class Visibility(StrEnum):
    PRIVATE = "private"
    ORGANIZATION = "organization"


class OrganizationRole(StrEnum):
    MEMBER = "member"
    ORGANIZATION_ADMIN = "organization_admin"


READABLE_DOCUMENT_SQL = """
    d.organization_id = ?
    AND d.deleted_at IS NULL
    AND (
        d.visibility = 'organization'
        OR d.owner_id = ?
        OR EXISTS (
            SELECT 1 FROM document_permissions dp
            WHERE dp.organization_id = d.organization_id
              AND dp.document_id = d.id
              AND dp.user_id = ?
              AND dp.permission IN ('read', 'manage')
        )
    )
"""

MANAGEABLE_DOCUMENT_SQL = """
    d.organization_id = ?
    AND d.deleted_at IS NULL
    AND (
        d.owner_id = ?
        OR ? = 'organization_admin'
        OR EXISTS (
            SELECT 1 FROM document_permissions dp
            WHERE dp.organization_id = d.organization_id
              AND dp.document_id = d.id
              AND dp.user_id = ?
              AND dp.permission = 'manage'
        )
    )
"""


def require_document(
    connection: sqlite3.Connection,
    document_id: int,
    user: dict[str, object],
    *,
    manage: bool = False,
    include_deleted: bool = False,
) -> sqlite3.Row:
    """Load an authorized document and return the same safe 404 for all denials."""
    predicate = MANAGEABLE_DOCUMENT_SQL if manage else READABLE_DOCUMENT_SQL
    if include_deleted:
        predicate = predicate.replace("AND d.deleted_at IS NULL", "")
    parameters: tuple[object, ...]
    if manage:
        parameters = (
            user["organization_id"], user["id"], user["role"], user["id"],
        )
    else:
        parameters = (
            user["organization_id"], user["id"], user["id"],
        )
    row = connection.execute(
        f"SELECT d.* FROM documents d WHERE d.id = ? AND {predicate}",
        (document_id, *parameters),
    ).fetchone()
    if row is None:
        log_event(
            "authorization.document.denied",
            organization_id=user.get("organization_id"),
            user_id=user.get("id"),
            document_id=document_id,
            permission="manage" if manage else "read",
        )
        raise HTTPException(status_code=404, detail="Document was not found.")
    return row
