"""SMTP email service tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.config import settings
from app.services.email import send_password_reset_email


class EmailServiceTests(unittest.TestCase):
    """Verify SMTP delivery behavior without contacting a provider."""

    def test_unconfigured_smtp_skips_delivery(self) -> None:
        """Missing SMTP credentials fail closed instead of attempting a network call."""
        with (
            patch.object(settings, "smtp_host", ""),
            patch("app.services.email.smtplib.SMTP") as smtp,
        ):
            sent = send_password_reset_email(
                recipient_email="owner@example.com",
                reset_url="http://localhost:5173?token=secret-token",
                expires_in_minutes=30,
            )

        self.assertFalse(sent)
        smtp.assert_not_called()

    def test_configured_smtp_sends_reset_message_with_tls(self) -> None:
        """Configured SMTP sends the reset email through STARTTLS."""
        smtp_client = MagicMock()
        smtp_context = MagicMock()
        smtp_context.__enter__.return_value = smtp_client
        with (
            patch.object(settings, "smtp_host", "smtp-relay.brevo.com"),
            patch.object(settings, "smtp_port", 587),
            patch.object(settings, "smtp_username", "brevo-user"),
            patch.object(settings, "smtp_password", "brevo-key"),
            patch.object(settings, "smtp_from_email", "no-reply@example.com"),
            patch.object(settings, "smtp_from_name", "Docsense AI"),
            patch.object(settings, "smtp_use_tls", True),
            patch("app.services.email.smtplib.SMTP", return_value=smtp_context) as smtp,
        ):
            sent = send_password_reset_email(
                recipient_email="owner@example.com",
                reset_url="http://localhost:5173?token=secret-token",
                expires_in_minutes=30,
            )

        self.assertTrue(sent)
        smtp.assert_called_once_with("smtp-relay.brevo.com", 587, timeout=settings.smtp_timeout_seconds)
        smtp_client.starttls.assert_called_once()
        smtp_client.login.assert_called_once_with("brevo-user", "brevo-key")
        message = smtp_client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "owner@example.com")
        self.assertIn("http://localhost:5173?token=secret-token", message.get_content())


if __name__ == "__main__":
    unittest.main()
