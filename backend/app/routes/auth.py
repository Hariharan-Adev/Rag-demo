"""Registration and JWT login endpoints."""

import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from app.auth import create_access_token, hash_password, verify_password
from app.database import get_connection
from app.utils.audit import log_audit_event

router = APIRouter(prefix="/auth", tags=["authentication"])


class RegisterRequest(BaseModel):
    """New local user registration."""

    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    organization_name: str = Field(default="My Organization", min_length=1, max_length=120)


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(
    api_request: Request,
    request: RegisterRequest,
) -> dict[str, object]:
    """Create a local user with a securely hashed password."""
    client_ip = api_request.client.host if api_request.client else ""

    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            organization_id = str(uuid4())
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                (organization_id, request.organization_name.strip()),
            )
            cursor = connection.execute(
                """INSERT INTO users
                   (email, password_hash, organization_id, role)
                   VALUES (?, ?, ?, 'organization_admin')""",
                (request.email.lower(), hash_password(request.password), organization_id),
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
        user_id=cursor.lastrowid,
        organization_id=organization_id,
        client_ip=client_ip,
    )

    return {
        "id": cursor.lastrowid,
        "email": request.email.lower(),
        "organization_id": organization_id,
    }


@router.post("/login")
def login_user(
    api_request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    x_organization_id: str | None = Header(default=None),
) -> dict[str, str]:
    """Return a JWT access token for valid credentials."""
    client_ip = api_request.client.host if api_request.client else ""

    with get_connection() as connection:
        users = connection.execute(
            """SELECT id, password_hash, organization_id
               FROM users
               WHERE email = ? AND deleted_at IS NULL
                 AND (? IS NULL OR organization_id = ?)""",
            (
                form_data.username.lower().strip(),
                x_organization_id,
                x_organization_id,
            ),
        ).fetchall()
    if len(users) > 1:
        raise HTTPException(
            status_code=409,
            detail="Organization identifier is required for this email.",
        )
    user = users[0] if users else None

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
