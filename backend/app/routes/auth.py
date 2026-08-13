"""Registration and JWT login endpoints."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from app.auth import create_access_token, hash_password, verify_password
from app.config import settings
from app.database import get_connection
from app.models.user_accounts import (
    active_email_exists,
    get_active_user_by_email,
    insert_user_account,
    normalize_email,
)
from app.services.email import send_password_reset_email
from app.utils.audit import log_audit_event
from app.utils.rate_limit import enforce_anonymous_request_limit

router = APIRouter(prefix="/auth", tags=["authentication"])
RESET_RESPONSE_MESSAGE = "If this email exists, we sent password reset instructions."


class RegisterRequest(BaseModel):
    """New local user registration."""

    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    organization_name: str = Field(default="My Organization", min_length=1, max_length=120)


class ForgotPasswordRequest(BaseModel):
    """Email address requesting password reset instructions."""

    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    """Generic reset-request response that does not expose account existence."""

    message: str


class ResetPasswordRequest(BaseModel):
    """One-time reset token and replacement password."""

    token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=12, max_length=128)


def _hash_reset_token(token: str) -> str:
    """Hash reset tokens before lookup so raw tokens never live in SQLite."""
    return sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    """Return timezone-aware UTC timestamps for token expiry checks."""
    return datetime.now(timezone.utc)


def _password_reset_url(token: str) -> str:
    """Build the frontend reset URL without logging or persisting the raw token."""
    base_url = settings.frontend_base_url.rstrip("/")
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}token={token}"


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(
    api_request: Request,
    request: RegisterRequest,
) -> dict[str, object]:
    """Create a local user with a securely hashed password."""
    client_ip = api_request.client.host if api_request.client else ""
    email = normalize_email(request.email)

    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if active_email_exists(connection, email):
                raise sqlite3.IntegrityError("active email already exists")
            organization_id = str(uuid4())
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                (organization_id, request.organization_name.strip()),
            )
            user_id = insert_user_account(
                connection,
                email=email,
                password_hash=hash_password(request.password),
                organization_id=organization_id,
                role="organization_admin",
            )
    except sqlite3.IntegrityError as error:
        log_audit_event(
            event_type="auth.register",
            endpoint="auth/register",
            outcome="duplicate_email",
            client_ip=client_ip,
        )
        raise HTTPException(status_code=409, detail="Email is already registered.") from error

    log_audit_event(
        event_type="auth.register",
        endpoint="auth/register",
        outcome="success",
        user_id=user_id,
        organization_id=organization_id,
        client_ip=client_ip,
    )

    return {
        "id": user_id,
        "email": email,
        "organization_id": organization_id,
    }


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(
    api_request: Request,
    request: ForgotPasswordRequest,
) -> ForgotPasswordResponse:
    """Create short-lived reset tokens without revealing account existence."""
    client_ip = api_request.client.host if api_request.client else ""
    email = normalize_email(request.email)
    enforce_anonymous_request_limit(
        client_ip,
        "auth/forgot-password",
        settings.password_reset_requests_per_hour,
        identifier=email,
    )

    expires_at = (_utc_now() + timedelta(minutes=settings.password_reset_token_minutes)).isoformat()

    reset_messages: list[tuple[int, str, str]] = []
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        user = get_active_user_by_email(connection, email)
        if user is not None:
            token = token_urlsafe(32)
            token_hash = _hash_reset_token(token)
            # Supersede older active reset links for this account so only the
            # latest request remains useful if multiple emails are sent.
            connection.execute(
                """UPDATE password_reset_tokens
                   SET used_at = COALESCE(used_at, ?)
                   WHERE organization_id = ? AND user_id = ? AND used_at IS NULL""",
                (_utc_now().isoformat(), user["organization_id"], user["id"]),
            )
            connection.execute(
                """INSERT INTO password_reset_tokens
                   (organization_id, user_id, token_hash, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (user["organization_id"], user["id"], token_hash, expires_at),
            )
            reset_messages.append((int(user["id"]), str(user["organization_id"]), token))

    email_sent = 0
    email_failed = 0
    for user_id, organization_id, token in reset_messages:
        sent = send_password_reset_email(
            recipient_email=email,
            reset_url=_password_reset_url(token),
            expires_in_minutes=settings.password_reset_token_minutes,
        )
        if sent:
            email_sent += 1
            continue
        email_failed += 1
        with get_connection() as connection:
            # Invalidate unsent reset tokens so an undelivered link cannot remain useful.
            connection.execute(
                """UPDATE password_reset_tokens
                   SET used_at = COALESCE(used_at, ?)
                   WHERE organization_id = ? AND user_id = ?
                     AND token_hash = ? AND used_at IS NULL""",
                (_utc_now().isoformat(), organization_id, user_id, _hash_reset_token(token)),
            )

    log_audit_event(
        event_type="auth.forgot_password",
        endpoint="auth/forgot-password",
        outcome="accepted",
        client_ip=client_ip,
        metadata={
            "matched_accounts": len(reset_messages),
            "email_sent": email_sent,
            "email_failed": email_failed,
        },
    )

    return ForgotPasswordResponse(message=RESET_RESPONSE_MESSAGE)


@router.post("/reset-password")
def reset_password(
    api_request: Request,
    request: ResetPasswordRequest,
) -> dict[str, str]:
    """Replace a password after validating an unused, unexpired reset token."""
    client_ip = api_request.client.host if api_request.client else ""
    enforce_anonymous_request_limit(
        client_ip,
        "auth/reset-password",
        settings.password_reset_requests_per_hour,
        identifier=request.token,
    )
    now = _utc_now().isoformat()
    token_hash = _hash_reset_token(request.token)

    reset_user_id: int | None = None
    reset_organization_id: str | None = None
    invalid_token = False
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        token = connection.execute(
            """SELECT password_reset_tokens.id, password_reset_tokens.user_id,
                      password_reset_tokens.organization_id
               FROM password_reset_tokens
               JOIN users ON users.id = password_reset_tokens.user_id
               WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                 AND users.deleted_at IS NULL""",
            (token_hash, now),
        ).fetchone()
        if token is None:
            invalid_token = True
        else:
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ? AND deleted_at IS NULL",
                (hash_password(request.new_password), token["user_id"]),
            )
            connection.execute(
                """UPDATE password_reset_tokens
                   SET used_at = ?
                   WHERE organization_id = ? AND user_id = ? AND used_at IS NULL""",
                (now, token["organization_id"], token["user_id"]),
            )
            reset_user_id = int(token["user_id"])
            reset_organization_id = str(token["organization_id"])

    if invalid_token:
        log_audit_event(
            event_type="auth.reset_password",
            endpoint="auth/reset-password",
            outcome="invalid_token",
            client_ip=client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset link is invalid or expired.",
        )

    log_audit_event(
        event_type="auth.reset_password",
        endpoint="auth/reset-password",
        outcome="success",
        user_id=reset_user_id,
        organization_id=reset_organization_id,
        client_ip=client_ip,
    )
    return {"message": "Password has been reset. You can now sign in."}


@router.post("/login")
def login_user(
    api_request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> dict[str, str]:
    """Return a JWT access token for valid credentials."""
    client_ip = api_request.client.host if api_request.client else ""

    with get_connection() as connection:
        user = get_active_user_by_email(connection, form_data.username)

    if user is None or not verify_password(form_data.password, user["password_hash"]):
        log_audit_event(
            event_type="auth.login",
            endpoint="auth/login",
            outcome="invalid_credentials",
            client_ip=client_ip,
        )
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    log_audit_event(
        event_type="auth.login",
        endpoint="auth/login",
        outcome="success",
        user_id=user["id"],
        organization_id=str(user["organization_id"]),
        client_ip=client_ip,
    )

    return {
        "access_token": create_access_token(user["id"], str(user["organization_id"])),
        "token_type": "bearer",
    }
