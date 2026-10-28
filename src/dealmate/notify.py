"""Email notifications through Gmail SMTP with an app password.

Without GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env, emails are only logged,
so the rest of the system can run before the Gmail account is set up.
"""

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("dealmate.notify")


def send_email(to: str, subject: str, body: str) -> None:
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not password:
        log.info("[email not configured] to=%s subject=%s\n%s", to, subject, body)
        return

    msg = EmailMessage()
    msg["From"] = f"DealMate <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
    except Exception:
        # A failed email must never break a deal in progress.
        log.exception("failed to send email to %s", to)
