"""Privacy-preserving security audit logging."""

from hashlib import sha256
from json import dumps

from app.config import settings
from app.database import DEFAULT_ORGANIZATION_ID, get_connection


def hash_ip(client_ip: str) -> str:
    """Hash IP addresses before logging them."""
    value = f"{settings.rate_limit_salt}:{client_ip}"
    return sha256(value.encode()).hexdigest()


def log_audit_event(
    *,
    event_type: str,
    endpoint: str,
    outcome: str,
    user_id: int | None = None,
    organization_id: str | None = None,
    client_ip: str = "",
    request_id: str | None = None,
    job_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """Record minimal operational evidence without sensitive content."""
    with get_connection() as connection:
        if organization_id is None and user_id is not None:
            user = connection.execute(
                "SELECT organization_id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            organization_id = str(user["organization_id"]) if user else None
        # Pre-authentication failures have no trusted tenant context. Keep them in
        # the reserved system/default tenant instead of accepting a request value.
        organization_id = organization_id or DEFAULT_ORGANIZATION_ID
        connection.execute(
            """
            INSERT INTO audit_events
                (user_id, organization_id, event_type, endpoint, outcome, ip_hash,
                 request_id, job_id, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                organization_id,
                event_type,
                endpoint,
                outcome,
                hash_ip(client_ip) if client_ip else None,
                request_id,
                job_id,
                dumps(metadata or {}),
            ),
        )
