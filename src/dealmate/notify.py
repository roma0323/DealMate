"""Email notifications through Gmail SMTP with an app password.

Without GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env, emails are only logged,
so the rest of the system can run before the Gmail account is set up.
"""

import html
import logging
import os
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

log = logging.getLogger("dealmate.notify")


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def calendar_invite(event: dict, organizer: str, attendee: str) -> str:
    """An iCalendar REQUEST for one event, so mail apps offer to add it to the calendar.

    `event`: uid, start (aware datetime), minutes, summary, location, description.
    Times go out in UTC, so no VTIMEZONE block is needed.
    """
    utc = "%Y%m%dT%H%M%SZ"
    start = event["start"].astimezone(timezone.utc)
    end = start + timedelta(minutes=event["minutes"])
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DealMate//DealMate//ZH-TW", "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{event['uid']}",
        f"SEQUENCE:{int(now.timestamp())}",  # grows on every reschedule, so calendars update the event
        f"DTSTAMP:{now.strftime(utc)}",
        f"DTSTART:{start.strftime(utc)}",
        f"DTEND:{end.strftime(utc)}",
        f"SUMMARY:{_ics_text(event['summary'])}",
        f"LOCATION:{_ics_text(event['location'])}",
        f"DESCRIPTION:{_ics_text(event['description'])}",
        f"ORGANIZER;CN=DealMate:mailto:{organizer}",
        f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE:mailto:{attendee}",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def build_message(
    sender: str, to: str, subject: str, body: str, image: Path | None = None, event: dict | None = None
) -> EmailMessage:
    """Plain-text email; with `image`, also an HTML version that shows the photo inline;
    with `event`, a calendar invite attached.

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
        from .photos import detect_type  # local import: photos imports db, notify stays light

        kind = detect_type(data) or ("jpg", "image/jpeg")
        maintype, subtype = kind[1].split("/")
        msg.get_payload()[1].add_related(data, maintype, subtype, cid=cid, filename=f"photo.{kind[0]}")
    if event is not None:
        msg.add_attachment(
            calendar_invite(event, sender, to).encode(), "text", "calendar",
            filename="invite.ics", params={"method": "REQUEST", "charset": "utf-8"},
        )
    return msg


def send_email(to: str, subject: str, body: str, image: Path | None = None, event: dict | None = None) -> bool:
    """Send one email. Returns True only if it was actually sent."""
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not sender or not password:
        log.info("[email not configured] to=%s subject=%s photo=%s invite=%s\n%s",
                 to, subject, image, bool(event), body)
        return False

    msg = build_message(sender, to, subject, body, image, event)
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


def send_email_async(to: str, subject: str, body: str, image: Path | None = None, event: dict | None = None) -> None:
    """Send in a background thread, so SMTP never holds up a tool call or a DB transaction."""
    threading.Thread(target=send_email, args=(to, subject, body, image, event), daemon=True).start()


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
