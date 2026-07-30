"""Password hashing, JWT creation, and current-user authentication."""

from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pwdlib import PasswordHash

from app.config import settings
from app.database import get_connection

password_hasher = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired authentication token.",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_password(password: str) -> str:
    """Hash a password before storage."""
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its stored hash."""
    return password_hasher.verify(password, password_hash)


def create_access_token(user_id: int, organization_id: str) -> str:
    """Create a short-lived JWT for one user."""
    if not settings.jwt_secret_key:
        raise ValueError("JWT_SECRET_KEY is not configured.")

    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )

    return jwt.encode(
        {"sub": str(user_id), "org": organization_id, "exp": expires_at},
        settings.jwt_secret_key,
        algorithm="HS256",
    )


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict[str, object]:
    """Validate the bearer token and load its user."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=["HS256"],
        )
        user_id = int(payload.get("sub", ""))
        token_organization_id = str(payload.get("org", ""))
    except (JWTError, ValueError, TypeError):
        raise CREDENTIALS_ERROR

    with get_connection() as connection:
        row = connection.execute(
            "SELECT id, email, organization_id, role FROM users WHERE id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()

    if row is None:
        raise CREDENTIALS_ERROR
    if token_organization_id and token_organization_id != row["organization_id"]:
        raise CREDENTIALS_ERROR

    return dict(row)
