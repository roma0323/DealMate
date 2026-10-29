"""Listing photos: validation and storage.

Photos arrive from the listing card's upload button (app_upload_photo) or, for
small images, from the upload_photo tool, and are stored under photos/.
"""

import logging
import os
import secrets
from pathlib import Path

from . import db

log = logging.getLogger("dealmate.photos")

PHOTOS_DIR = Path(os.environ.get("DEALMATE_PHOTOS", "photos"))
MAX_BYTES = 8 * 1024 * 1024

# Magic bytes -> (extension, MIME type)
_SIGNATURES = [
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF8", "gif", "image/gif"),
]


def detect_type(data: bytes) -> tuple[str, str] | None:
    for magic, ext, mime in _SIGNATURES:
        if data.startswith(magic):
            return ext, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def ensure_upload_token(conn, listing_id: int) -> str:
    listing = db.row(conn, "SELECT upload_token FROM listings WHERE id = ?", listing_id)
    if listing["upload_token"]:
        return listing["upload_token"]
    token = secrets.token_urlsafe(16)
    conn.execute("UPDATE listings SET upload_token = ? WHERE id = ?", (token, listing_id))
    return token


def save_photo(conn, listing_id: int, data: bytes) -> None:
    """Validate and store a listing photo. Raises ValueError on bad input."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"照片太大（上限 {MAX_BYTES // 1024 // 1024} MB）。")
    kind = detect_type(data)
    if kind is None:
        raise ValueError("看不出這是圖片，請上傳 JPG、PNG、GIF 或 WebP。")
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    for old in PHOTOS_DIR.glob(f"listing-{listing_id}.*"):
        old.unlink()
    path = PHOTOS_DIR / f"listing-{listing_id}.{kind[0]}"
    path.write_bytes(data)
    conn.execute("UPDATE listings SET photo_path = ? WHERE id = ?", (str(path), listing_id))
    log.info("saved photo for listing %s (%d bytes)", listing_id, len(data))


def photo_file(listing: dict) -> Path | None:
    path = listing.get("photo_path")
    return Path(path) if path and Path(path).exists() else None
