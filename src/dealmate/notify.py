"""Email notifications through Gmail SMTP with an app password.

Without GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env, emails are only logged,
so the rest of the system can run before the Gmail account is set up.
"""

import logging
import os
import smtplib
import threading
from email.message import EmailMessage

log = logging.getLogger("dealmate.notify")


def send_email(to: str, subject: str, body: str) -> bool:
    """Send one email. Returns True only if it was actually sent."""
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not sender or not password:
        log.info("[email not configured] to=%s subject=%s\n%s", to, subject, body)
        return False

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
        return False
    log.info("email sent to %s: %s", to, subject)
    return True


def send_email_async(to: str, subject: str, body: str) -> None:
    """Send in a background thread, so SMTP never holds up a tool call or a DB transaction."""
    threading.Thread(target=send_email, args=(to, subject, body), daemon=True).start()


if __name__ == "__main__":
    # Check the Gmail setup: uv run python -m dealmate.notify you@example.com
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) != 2:
        sys.exit("usage: uv run python -m dealmate.notify <to-address>")
    ok = send_email(sys.argv[1], "DealMate 測試信", "如果你收到這封信，DealMate 的 email 通知已經設定成功。")
    sys.exit(0 if ok else 1)
