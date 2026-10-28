"""Listing photos: storage, the upload page, and the public photo URL.

Claude can't hand a chat attachment to a tool as a file, so a seller gets an
upload link (create_listing returns it) and opens it on their phone or computer.
The page shrinks the photo in the browser and POSTs the raw JPEG back here.
Small images can also arrive through the upload_photo tool as base64.
"""

import logging
import os
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

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


def base_url() -> str:
    host = os.environ.get("PUBLIC_HOST", "").strip()
    return f"https://{host}" if host else f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"


def photo_url(listing_id: int) -> str:
    return f"{base_url()}/photos/{listing_id}"


def upload_url(token: str) -> str:
    return f"{base_url()}/upload/{token}"


def ensure_upload_token(conn, listing_id: int) -> str:
    listing = db.row(conn, "SELECT upload_token FROM listings WHERE id = ?", listing_id)
    if listing["upload_token"]:
        return listing["upload_token"]
    token = secrets.token_urlsafe(16)
    conn.execute("UPDATE listings SET upload_token = ? WHERE id = ?", (token, listing_id))
    return token


def save_photo(conn, listing_id: int, data: bytes) -> str:
    """Validate and store a listing photo. Returns its public URL. Raises ValueError on bad input."""
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
    return photo_url(listing_id)


def photo_file(listing: dict) -> Path | None:
    path = listing.get("photo_path")
    return Path(path) if path and Path(path).exists() else None


# ---------- web routes ----------

_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DealMate 上傳照片</title>
<style>
  :root {{ color-scheme: light dark; --accent: #2f6fde; }}
  body {{ font-family: -apple-system, "PingFang TC", "Noto Sans TC", sans-serif; margin: 0;
         padding: 24px 16px; max-width: 480px; margin-inline: auto; line-height: 1.5; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 4px; }}
  .item {{ color: #888; margin: 0 0 20px; }}
  label.pick {{ display: block; border: 2px dashed #8884; border-radius: 12px; padding: 32px 16px;
               text-align: center; cursor: pointer; }}
  input[type=file] {{ display: none; }}
  img {{ max-width: 100%; border-radius: 12px; margin-top: 16px; display: none; }}
  button {{ width: 100%; margin-top: 16px; padding: 14px; font-size: 1rem; border: 0;
           border-radius: 10px; background: var(--accent); color: #fff; display: none; }}
  #msg {{ margin-top: 16px; font-weight: 600; }}
</style></head>
<body>
<h1>上傳商品照片</h1>
<p class="item">{title}</p>
<label class="pick" for="file">點這裡拍照或選擇照片</label>
<input id="file" type="file" accept="image/*">
<img id="preview" alt="預覽">
<button id="send">上傳</button>
<p id="msg"></p>
<script>
const file = document.getElementById('file'), img = document.getElementById('preview'),
      send = document.getElementById('send'), msg = document.getElementById('msg');
let blob = null;
file.onchange = () => {{
  const f = file.files[0]; if (!f) return;
  const src = new Image();
  src.onload = () => {{
    // Shrink in the browser so phone photos upload quickly.
    const scale = Math.min(1, 1600 / Math.max(src.width, src.height));
    const c = document.createElement('canvas');
    c.width = Math.round(src.width * scale); c.height = Math.round(src.height * scale);
    c.getContext('2d').drawImage(src, 0, 0, c.width, c.height);
    c.toBlob(b => {{ blob = b; img.src = URL.createObjectURL(b); img.style.display = 'block';
                    send.style.display = 'block'; msg.textContent = ''; }}, 'image/jpeg', 0.85);
  }};
  src.src = URL.createObjectURL(f);
}};
send.onclick = async () => {{
  if (!blob) return;
  send.disabled = true; msg.textContent = '上傳中…';
  try {{
    const r = await fetch(location.pathname, {{ method: 'POST', body: blob,
                                               headers: {{ 'Content-Type': 'image/jpeg' }} }});
    const j = await r.json();
    msg.textContent = r.ok ? '上傳完成！可以回到 Claude 了。' : ('上傳失敗：' + j.error);
  }} catch (e) {{ msg.textContent = '上傳失敗，請再試一次。'; }}
  send.disabled = false;
}};
</script>
</body></html>"""


def _listing_by_token(token: str) -> dict | None:
    with db.connect() as conn:
        return db.row(conn, "SELECT id, title FROM listings WHERE upload_token = ?", token)


def register_routes(mcp) -> None:
    @mcp.custom_route("/upload/{token}", methods=["GET"])
    async def upload_page(request: Request) -> Response:
        listing = _listing_by_token(request.path_params["token"])
        if listing is None:
            return HTMLResponse("<p>這個上傳連結無效。</p>", status_code=404)
        title = listing["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return HTMLResponse(_PAGE.format(title=title))

    @mcp.custom_route("/upload/{token}", methods=["POST"])
    async def upload_post(request: Request) -> Response:
        listing = _listing_by_token(request.path_params["token"])
        if listing is None:
            return JSONResponse({"error": "這個上傳連結無效。"}, status_code=404)
        data = await request.body()
        try:
            with db.connect() as conn:
                url = save_photo(conn, listing["id"], data)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"photo_url": url})

    @mcp.custom_route("/photos/{listing_id:int}", methods=["GET"])
    async def photo(request: Request) -> Response:
        with db.connect() as conn:
            listing = db.row(conn, "SELECT photo_path FROM listings WHERE id = ?", request.path_params["listing_id"])
        path = photo_file(listing) if listing else None
        if path is None:
            return Response("找不到照片", status_code=404)
        return FileResponse(path, media_type=detect_type(path.read_bytes()[:16])[1])
