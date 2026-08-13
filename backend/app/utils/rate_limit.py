"""SQLite-backed request and Groq usage limits for the local MVP."""

from datetime import datetime, timezone
from hashlib import sha256
from time import time

from fastapi import HTTPException, status

from app.config import settings
from app.database import DEFAULT_ORGANIZATION_ID, get_connection
from app.utils.audit import log_audit_event

WINDOW_SECONDS = 60 * 60  # One-hour fixed window.


def _ip_scope(client_ip: str) -> str:
    """Hash the IP so raw addresses are not stored in SQLite."""
    value = f"{settings.rate_limit_salt}:{client_ip}"
    return f"ip:{sha256(value.encode()).hexdigest()}"


def _anonymous_scope(kind: str, value: str) -> str:
    """Hash pre-authentication identifiers before using them as quota keys."""
    normalized = value.lower().strip()
    digest = sha256(f"{settings.rate_limit_salt}:{kind}:{normalized}".encode()).hexdigest()
    return f"{kind}:{digest}"


def enforce_anonymous_request_limit(
    client_ip: str,
    endpoint: str,
    maximum: int,
    *,
    identifier: str = "",
) -> None:
    """Limit unauthenticated flows by IP and optional account identifier."""
    window_start = int(time() // WINDOW_SECONDS) * WINDOW_SECONDS
    scopes = [_ip_scope(client_ip)]
    if identifier:
        scopes.append(_anonymous_scope("account", identifier))
    blocked = False

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for scope in scopes:
            row = connection.execute(
                """
                SELECT request_count
                FROM rate_limit_windows
                WHERE scope = ? AND endpoint = ? AND window_start = ?
                  AND organization_id = ?
                """,
                (scope, endpoint, window_start, DEFAULT_ORGANIZATION_ID),
            ).fetchone()
            if row is not None and row["request_count"] >= maximum:
                blocked = True
                break

        if not blocked:
            for scope in scopes:
                connection.execute(
                    """
                    INSERT INTO rate_limit_windows
                        (organization_id, scope, endpoint, window_start, request_count)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(organization_id, scope, endpoint, window_start)
                    DO UPDATE SET request_count = request_count + 1
                    """,
                    (DEFAULT_ORGANIZATION_ID, scope, endpoint, window_start),
                )

    if blocked:
        log_audit_event(
            event_type="rate_limit.blocked",
            endpoint=endpoint,
            outcome="blocked",
            client_ip=client_ip,
            metadata={"category": "pre_auth_hourly_request"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Request limit exceeded. Try again later.",
        )


def enforce_request_limit(
    user_id: int,
    client_ip: str,
    endpoint: str,
    maximum: int,
) -> None:
    """Atomically enforce matching user and IP hourly limits."""
    window_start = int(time() // WINDOW_SECONDS) * WINDOW_SECONDS
    scopes = [f"user:{user_id}", _ip_scope(client_ip)]
    blocked = False

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        organization_id = connection.execute(
            "SELECT organization_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()["organization_id"]

        for scope in scopes:
            row = connection.execute(
                """
                SELECT request_count
                FROM rate_limit_windows
                WHERE scope = ? AND endpoint = ? AND window_start = ?
                  AND organization_id = ?
                """,
                (scope, endpoint, window_start, organization_id),
            ).fetchone()

            if row is not None and row["request_count"] >= maximum:
                blocked = True
                break

        if not blocked:
            for scope in scopes:
                connection.execute(
                    """
                    INSERT INTO rate_limit_windows
                        (scope, endpoint, window_start, request_count, organization_id)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(organization_id, scope, endpoint, window_start)
                    DO UPDATE SET request_count = request_count + 1
                    """,
                    (scope, endpoint, window_start, organization_id),
                )

    if blocked:
        log_audit_event(
            event_type="rate_limit.blocked",
            endpoint=endpoint,
            outcome="blocked",
            user_id=user_id,
            client_ip=client_ip,
            metadata={"category": "hourly_request"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Request limit exceeded. Try again later.",
        )


def reserve_groq_call(user_id: int, client_ip: str = "") -> None:
    """Reserve one daily Groq call before the provider is contacted."""
    usage_date = datetime.now(timezone.utc).date().isoformat()
    blocked = False

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        organization_id = connection.execute(
            "SELECT organization_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()["organization_id"]

        row = connection.execute(
            """
            SELECT request_count, prompt_tokens, completion_tokens FROM llm_usage
            WHERE user_id = ? AND usage_date = ? AND organization_id = ?
            """,
            (user_id, usage_date, organization_id),
        ).fetchone()

        if row is not None:
            token_total = int(row["prompt_tokens"]) + int(row["completion_tokens"])
            estimated_cost = (
                int(row["prompt_tokens"]) * settings.groq_prompt_cost_per_million
                + int(row["completion_tokens"]) * settings.groq_completion_cost_per_million
            ) / 1_000_000
            blocked = (
                int(row["request_count"]) >= settings.groq_calls_per_day
                or token_total >= settings.groq_daily_token_budget
                or estimated_cost >= settings.groq_daily_cost_cap_usd
            )
        else:
            connection.execute(
                """
                INSERT INTO llm_usage
                    (user_id, usage_date, request_count, organization_id)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(organization_id, user_id, usage_date)
                DO UPDATE SET request_count = request_count + 1
                """,
                (user_id, usage_date, organization_id),
            )

    if blocked:
        log_audit_event(
            event_type="chat.request",
            endpoint="chat",
            outcome="quota_blocked",
            user_id=user_id,
            client_ip=client_ip,
            metadata={"reason": "daily_groq_quota"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily AI usage limit exceeded.",
        )


def record_groq_tokens(
    user_id: int,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Record provider token counts without logging conversation content."""
    usage_date = datetime.now(timezone.utc).date().isoformat()

    with get_connection() as connection:
        organization_id = connection.execute(
            "SELECT organization_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()["organization_id"]
        connection.execute(
            """
            UPDATE llm_usage
            SET
                prompt_tokens = prompt_tokens + ?,
                completion_tokens = completion_tokens + ?
            WHERE organization_id = ? AND user_id = ? AND usage_date = ?
            """,
            (
                prompt_tokens, completion_tokens, organization_id,
                user_id, usage_date,
            ),
        )
