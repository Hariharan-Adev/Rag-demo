"""SMTP email delivery helpers for account recovery."""

from __future__ import annotations

from email.message import EmailMessage
from logging import getLogger
import smtplib

from app.config import settings

logger = getLogger(__name__)


def _smtp_configured() -> bool:
    """Require every secret-bearing SMTP field before attempting delivery."""
    return all(
        [
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_username,
            settings.smtp_password,
            settings.smtp_from_email,
        ]
    )


def send_password_reset_email(
    *,
    recipient_email: str,
    reset_url: str,
    expires_in_minutes: int,
) -> bool:
    """Send a password reset link through the configured SMTP provider."""
    if not _smtp_configured():
        logger.warning("Password reset email skipped because SMTP is not configured.")
        return False

    message = EmailMessage()
    message["Subject"] = "Reset your Docsense AI password"
    message["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    message["To"] = recipient_email
    message.set_content(
        "\n".join(
            [
                "Use this link to reset your Docsense AI password:",
                "",
                reset_url,
                "",
                f"This link expires in {expires_in_minutes} minutes.",
                "If you did not request this, you can ignore this email.",
            ]
        )
    )

    try:
        with smtplib.SMTP(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
        ) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        # Do not include SMTP credentials, recipient addresses, or reset tokens in logs.
        logger.warning("Password reset email delivery failed.")
        return False

    return True
