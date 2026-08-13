"""Password reset endpoint regression tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database
from app.main import app


class PasswordResetFlowTests(unittest.TestCase):
    """Exercise forgot-password and reset-password with mocked SMTP delivery."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "auth.db"
        self.db_patch = patch.object(database, "DATABASE_PATH", self.database_path)
        self.db_patch.start()
        database.initialize_database()
        self.client = TestClient(app)
        self.client.post(
            "/auth/register",
            json={
                "email": "owner@example.com",
                "password": "original-password",
                "organization_name": "Owner Org",
            },
        )

    def tearDown(self) -> None:
        self.client.close()
        self.db_patch.stop()
        self.temporary.cleanup()

    def login(self, password: str):
        """Submit the OAuth password form used by the React login screen."""
        return self.client.post(
            "/auth/login",
            data={"username": "owner@example.com", "password": password},
        )

    def request_reset_token(self) -> str:
        """Request reset while controlling the generated one-time token."""
        with (
            patch("app.routes.auth.token_urlsafe", return_value="known-reset-token-value-1234567890"),
            patch("app.routes.auth.send_password_reset_email", return_value=True) as mailer,
        ):
            response = self.client.post(
                "/auth/forgot-password",
                json={"email": "owner@example.com"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"message": "If this email exists, we sent password reset instructions."},
        )
        mailer.assert_called_once()
        reset_url = mailer.call_args.kwargs["reset_url"]
        self.assertIn("?token=known-reset-token-value-1234567890", reset_url)
        return "known-reset-token-value-1234567890"

    def test_forgot_password_uses_generic_response_for_unknown_email(self) -> None:
        """Unknown emails get the same safe message and no reset token."""
        with patch("app.routes.auth.send_password_reset_email") as mailer:
            response = self.client.post(
                "/auth/forgot-password",
                json={"email": "missing@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["message"],
            "If this email exists, we sent password reset instructions.",
        )
        self.assertNotIn("reset_token", response.json())
        mailer.assert_not_called()

    def test_failed_email_delivery_invalidates_created_token(self) -> None:
        """Undelivered reset links are invalidated without changing the response."""
        with (
            patch("app.routes.auth.token_urlsafe", return_value="known-reset-token-value-1234567890"),
            patch("app.routes.auth.send_password_reset_email", return_value=False),
        ):
            response = self.client.post(
                "/auth/forgot-password",
                json={"email": "owner@example.com"},
            )

        reset = self.client.post(
            "/auth/reset-password",
            json={
                "token": "known-reset-token-value-1234567890",
                "new_password": "new-password-value",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(reset.status_code, 400)

    def test_valid_reset_token_changes_password_and_invalidates_token(self) -> None:
        """A valid token changes the password once and blocks reuse."""
        token = self.request_reset_token()

        reset = self.client.post(
            "/auth/reset-password",
            json={"token": token, "new_password": "new-password-value"},
        )
        reused = self.client.post(
            "/auth/reset-password",
            json={"token": token, "new_password": "another-password"},
        )

        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reused.status_code, 400)
        self.assertEqual(self.login("original-password").status_code, 401)
        self.assertEqual(self.login("new-password-value").status_code, 200)

    def test_expired_reset_token_is_rejected(self) -> None:
        """Expired tokens fail without changing the user's password."""
        token = self.request_reset_token()
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE password_reset_tokens SET expires_at = '2000-01-01T00:00:00+00:00'"
            )

        response = self.client.post(
            "/auth/reset-password",
            json={"token": token, "new_password": "new-password-value"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.login("original-password").status_code, 200)


if __name__ == "__main__":
    unittest.main()
