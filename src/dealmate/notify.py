"""Email notifications through Gmail SMTP with an app password.

Without GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env, emails are only logged,
so the rest of the system can run before the Gmail account is set up.
"""

import html
import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

log = logging.getLogger("dealmate.notify")


def build_message(sender: str, to: str, subject: str, body: str, image: Path | None = None) -> EmailMessage:
    """Plain-text email; with `image`, also an HTML version that shows the photo inline.

    The photo travels inside the email, so it shows even when the laptop is off.
    """
    msg = EmailMessage()
    msg["From"] = f"DealMate <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if image is not None:
        cid = make_msgid(domain="dealmate")
        text = html.escape(body).replace("\n", "<br>")
        msg.add_alternative(
            f'<p>{text}</p><p><img src="cid:{cid[1:-1]}" alt="商品照片" style="max-width:480px;border-radius:8px"></p>',
            subtype="html",
        )
        data = image.read_bytes()
        from .photos import detect_type  # local import: photos imports starlette, notify stays light

        kind = detect_type(data) or ("jpg", "image/jpeg")
        maintype, subtype = kind[1].split("/")
        msg.get_payload()[1].add_related(data, maintype, subtype, cid=cid, filename=f"photo.{kind[0]}")
    return msg


def send_email(to: str, subject: str, body: str, image: Path | None = None) -> bool:
    """Send one email. Returns True only if it was actually sent."""
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not sender or not password:
        log.info("[email not configured] to=%s subject=%s photo=%s\n%s", to, subject, image, body)
        return False

    msg = build_message(sender, to, subject, body, image)
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


def send_email_async(to: str, subject: str, body: str, image: Path | None = None) -> None:
    """Send in a background thread, so SMTP never holds up a tool call or a DB transaction."""
    threading.Thread(target=send_email, args=(to, subject, body, image), daemon=True).start()


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
