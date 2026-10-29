"""DealMate MCP server: the tools Claude calls through the custom connector."""

import base64
import binascii
import logging
import os
from typing import Literal

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings

from . import db, engine, photos, ui
from .ui import ui_meta

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

mcp = MCPServer(
    name="DealMate",
    instructions=(
        "DealMate gives each user an AI agent that buys or sells second-hand items for them. "
        "The user's agent negotiates with the other side's agent on the DealMate server; "
        "the user only describes what they want, answers questions when asked, and confirms the final deal. "
        "Always call register_user first with the user's name and email if they have not registered. "
        "Pass the same `user` name on every later call. Reply to the user in Traditional Chinese. "
        "Sellers add a photo with the upload button on the listing card that create_listing shows."
    ),
    extensions=[ui.apps],
)


def _require_user(conn, user: str) -> dict:
    u = db.get_user(conn, user)
    if u is None:
        raise ToolError(f"找不到使用者「{user}」，請先呼叫 register_user。")
    return u


@mcp.tool()
def register_user(name: str, email: str) -> dict:
    """Register a DealMate user, or update their email if the name already exists.

    Call this before anything else. Ask the user for a display name and an email
    address; DealMate emails them when a deal is agreed or when their agent needs them.
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users (name, email) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET email = excluded.email",
            (name, email),
        )
        u = db.get_user(conn, name)
    return {"user_id": u["id"], "name": u["name"], "email": u["email"]}


@mcp.tool(meta=ui_meta())
def create_listing(
    user: str,
    category: str,
    title: str,
    ask_price: int,
    floor_price: int,
    auto_accept_price: int,
    condition: str = "",
    location: str = "",
) -> dict:
    """Seller: put an item up for sale and let the seller's agent negotiate for it.

    If the user attached a photo, describe the item from the photo into `title` and
    `condition`. Before calling, ask for anything missing, especially:
    - ask_price: the price to start from
    - floor_price: the lowest price they will ever accept (kept secret from the buyer)
    - auto_accept_price: at or above this, the agent may agree without asking them;
      below it (but above the floor), the agent will ask them first
    `category` is a short keyword used for matching, e.g. "水壺", "耳機", "書".

    The result is shown as a listing card with an upload button, so the seller can add
    a photo right in the chat. Tell the seller to use that button.
    """
    if not (floor_price <= auto_accept_price <= ask_price):
        raise ToolError("價格需符合：底價 ≤ 自動接受價 ≤ 開價。")
    with db.connect() as conn:
        seller = _require_user(conn, user)
        cur = conn.execute(
            "INSERT INTO listings (seller_id, category, title, condition, location, ask_price, floor_price, "
            "auto_accept_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (seller["id"], category, title, condition, location, ask_price, floor_price, auto_accept_price),
        )
        listing_id = cur.lastrowid
        token = photos.ensure_upload_token(conn, listing_id)
        neg_id = engine.find_match_for_listing(conn, listing_id)
    if neg_id:
        engine.start_negotiation_async(neg_id)
    with db.connect() as conn:
        listing = _public_listing(conn, listing_id)
    return {
        "view": "listing",
        "headline": "上架成功",
        "listing_id": listing_id,
        "matched": bool(neg_id),
        "negotiation_id": neg_id,
        "listing": listing,
        "upload_token": token,
    }


def _public_listing(conn, listing_id: int) -> dict | None:
    """What anyone may see about a listing: no floor price, auto-accept price or email."""
    l = db.row(
        conn,
        "SELECT l.id, l.title, l.category, l.condition, l.location, l.ask_price, l.status, l.created_at, "
        "l.photo_path, s.name AS seller FROM listings l JOIN users s ON s.id = l.seller_id WHERE l.id = ?",
        listing_id,
    )
    if l is None:
        return None
    l["has_photo"] = photos.photo_file(l) is not None
    del l["photo_path"]
    return l


@mcp.tool(meta=ui_meta())
def show_listing(user: str, listing_id: int) -> dict:
    """Show one item as a card with its photo and details.

    For the item's seller the card also has an upload button for adding or changing the photo.
    """
    with db.connect() as conn:
        u = _require_user(conn, user)
        listing = _public_listing(conn, listing_id)
        if listing is None:
            raise ToolError("找不到這件商品。")
        mine = db.row(conn, "SELECT 1 AS yes FROM listings WHERE id = ? AND seller_id = ?", listing_id, u["id"])
        token = photos.ensure_upload_token(conn, listing_id) if mine else None
    result = {"view": "listing", "listing": listing}
    if token:
        result["upload_token"] = token
    return result


@mcp.tool(meta=ui_meta(app_only=True))
def app_get_photo(listing_id: int) -> dict:
    """Used by the DealMate card to display a listing photo. Not for the model."""
    with db.connect() as conn:
        listing = db.row(conn, "SELECT photo_path FROM listings WHERE id = ?", listing_id)
    path = photos.photo_file(listing) if listing else None
    if path is None:
        raise ToolError("這件商品還沒有照片。")
    data = path.read_bytes()
    mime = photos.detect_type(data[:16])[1]
    return {"listing_id": listing_id, "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}


@mcp.tool(meta=ui_meta(app_only=True))
def app_upload_photo(upload_token: str, image_base64: str) -> dict:
    """Used by the DealMate card's upload button. Not for the model."""
    try:
        data = base64.b64decode(image_base64.split(",")[-1])
    except (binascii.Error, ValueError):
        raise ToolError("圖片內容無法解讀。")
    with db.connect() as conn:
        listing = db.row(conn, "SELECT id FROM listings WHERE upload_token = ?", upload_token)
        if listing is None:
            raise ToolError("上傳代碼無效。")
        try:
            photos.save_photo(conn, listing["id"], data)
        except ValueError as e:
            raise ToolError(str(e))
    return {"listing_id": listing["id"], "has_photo": True}


@mcp.tool()
def upload_photo(user: str, listing_id: int, image_base64: str) -> dict:
    """Seller: attach a photo to their listing by sending the image as base64.

    Only practical for small images. If the image is large, or this fails, ask the
    seller to use the upload button on the listing card (show_listing) instead.
    """
    try:
        data = base64.b64decode(image_base64.split(",")[-1], validate=False)
    except (binascii.Error, ValueError):
        raise ToolError("圖片的 base64 內容無法解讀，請改用商品卡片上的上傳按鈕。")
    with db.connect() as conn:
        seller = _require_user(conn, user)
        listing = db.row(conn, "SELECT id FROM listings WHERE id = ? AND seller_id = ?", listing_id, seller["id"])
        if listing is None:
            raise ToolError("找不到這位賣家的這件商品。")
        try:
            photos.save_photo(conn, listing_id, data)
        except ValueError as e:
            raise ToolError(f"{e} 也可以改用商品卡片上的上傳按鈕。")
    return {"listing_id": listing_id, "has_photo": True}


def _viewable_photo(user: str, listing_id: int) -> tuple[dict, bytes]:
    """The listing and its photo bytes, if `user` is its seller or a buyer negotiating for it."""
    with db.connect() as conn:
        u = _require_user(conn, user)
        listing = db.row(
            conn,
            """
            SELECT l.* FROM listings l
            WHERE l.id = ? AND (l.seller_id = ? OR EXISTS (
                SELECT 1 FROM negotiations n JOIN wants w ON w.id = n.want_id
                WHERE n.listing_id = l.id AND w.buyer_id = ?))
            """,
            listing_id, u["id"], u["id"],
        )
    if listing is None:
        raise ToolError("找不到這件商品，或您沒有在跟它議價。")
    path = photos.photo_file(listing)
    if path is None:
        raise ToolError("這件商品還沒有照片。")
    return listing, path.read_bytes()


@mcp.tool()
def view_listing_photo_for_ai(user: str, listing_id: int) -> Image:
    """Look at a listing's photo yourself, so you can describe the item to the user.

    Returns the image for you (the AI) to see; it is not shown to the user as a file.
    To show the photo to the user, use view_listing_photo_for_human.
    The seller can view their own listings; a buyer can view a listing they are negotiating for.
    """
    _, data = _viewable_photo(user, listing_id)
    return Image(data=data, format=photos.detect_type(data[:16])[0].replace("jpg", "jpeg"))


@mcp.tool()
def view_listing_photo_for_human(user: str, listing_id: int) -> dict:
    """Download a listing's photo as a base64 encoded string, to show it to the user.

    Same shape as a Drive file download: `content` is the file's bytes in base64,
    with `mimeType` and `title` (a file name). Decode `content` into a file named
    `title` and present that file to the user so they can see the photo.
    To look at the photo yourself instead, use view_listing_photo_for_ai.
    The seller can view their own listings; a buyer can view a listing they are negotiating for.
    """
    listing, data = _viewable_photo(user, listing_id)
    ext, mime = photos.detect_type(data[:16])
    return {
        "content": base64.b64encode(data).decode(),
        "id": f"listing-{listing_id}",
        "mimeType": mime,
        "title": f"{listing['title']}.{ext}",
    }


@mcp.tool()
def create_want(user: str, category: str, description: str, budget: int, location: str = "") -> dict:
    """Buyer: describe something to buy and let the buyer's agent find and negotiate for it.

    `budget` is the most the buyer will pay (kept secret from the seller).
    `category` is a short keyword used for matching, e.g. "水壺", "耳機", "書".
    """
    with db.connect() as conn:
        buyer = _require_user(conn, user)
        cur = conn.execute(
            "INSERT INTO wants (buyer_id, category, description, location, budget) VALUES (?, ?, ?, ?, ?)",
            (buyer["id"], category, description, location, budget),
        )
        want_id = cur.lastrowid
        neg_id = engine.find_match_for_want(conn, want_id)
    if neg_id:
        engine.start_negotiation_async(neg_id)
    return {"want_id": want_id, "matched": bool(neg_id), "negotiation_id": neg_id}


DEBUG = os.environ.get("DEALMATE_DEBUG", "").strip().lower() in ("1", "true", "yes")


@mcp.tool(meta=ui_meta())
def browse_listings(include_closed: bool = False) -> dict:
    """Browse every item on DealMate with its public details.
    The result is shown as product cards with photos.

    By default only items still for sale or being negotiated are shown; set
    include_closed to also show sold and closed ones. Use this when the user wants
    to see what is available. (With DEALMATE_DEBUG=1 on the server, the result also
    includes private fields for debugging: floor and auto-accept prices, emails, wants and negotiations.)
    """
    status_filter = "" if include_closed else "WHERE l.status IN ('open', 'matched')"
    with db.connect() as conn:
        listings = db.rows(
            conn,
            f"""
            SELECT l.id, l.title, l.category, l.condition, l.location, l.ask_price, l.status,
                   l.created_at, l.photo_path, l.floor_price, l.auto_accept_price,
                   s.name AS seller, s.email AS seller_email
            FROM listings l JOIN users s ON s.id = l.seller_id
            {status_filter}
            ORDER BY l.id
            """,
        )
        for l in listings:
            l["has_photo"] = photos.photo_file(l) is not None
            del l["photo_path"]
            if DEBUG:
                l["negotiations"] = db.rows(
                    conn,
                    """
                    SELECT n.id, n.state, n.round, n.seller_offer, n.buyer_offer, n.agreed_price,
                           b.name AS buyer, w.budget
                    FROM negotiations n JOIN wants w ON w.id = n.want_id JOIN users b ON b.id = w.buyer_id
                    WHERE n.listing_id = ? ORDER BY n.id
                    """,
                    l["id"],
                )
            else:
                # Private to the seller: never shown to other users.
                for key in ("floor_price", "auto_accept_price", "seller_email"):
                    del l[key]
        result = {"view": "gallery", "count": len(listings), "listings": listings}
        if DEBUG:
            result["debug"] = True
            result["wants"] = db.rows(
                conn,
                "SELECT w.id, w.category, w.description, w.budget, w.status, b.name AS buyer, b.email AS buyer_email "
                "FROM wants w JOIN users b ON b.id = w.buyer_id ORDER BY w.id",
            )
            result["users"] = db.rows(conn, "SELECT id, name, email, created_at FROM users ORDER BY id")
    return result


@mcp.tool()
def check_status(user: str) -> dict:
    """Show the user's listings, wants and negotiations, including the offer history
    and any question their agent is waiting for them to answer.

    Once a price is agreed, `my_report` holds the agent's report for this user:
    show it to them and ask them to confirm with confirm_deal.
    Call this whenever the user asks how things are going.
    """
    with db.connect() as conn:
        u = _require_user(conn, user)
        listings = db.rows(
            conn,
            "SELECT id, category, title, ask_price, floor_price, auto_accept_price, status, photo_path "
            "FROM listings WHERE seller_id = ?",
            u["id"],
        )
        for l in listings:
            l["has_photo"] = photos.photo_file(l) is not None
            del l["photo_path"]
        wants = db.rows(
            conn, "SELECT id, category, description, budget, status FROM wants WHERE buyer_id = ?", u["id"]
        )
        negs = db.rows(
            conn,
            """
            SELECT n.id, n.state, n.round, n.agreed_price, n.pending_party, n.pending_question,
                   n.seller_confirmed, n.buyer_confirmed, n.listing_id, l.title, l.photo_path,
                   CASE WHEN l.seller_id = ? THEN 'seller' ELSE 'buyer' END AS my_role,
                   CASE WHEN l.seller_id = ? THEN n.seller_report ELSE n.buyer_report END AS my_report
            FROM negotiations n
            JOIN listings l ON l.id = n.listing_id
            JOIN wants w ON w.id = n.want_id
            WHERE l.seller_id = ? OR w.buyer_id = ?
            ORDER BY n.id
            """,
            u["id"], u["id"], u["id"], u["id"],
        )
        for n in negs:
            n["has_photo"] = photos.photo_file(n) is not None
            del n["photo_path"]
            # Only show a pending question to the party it is addressed to.
            n["waiting_for_me"] = n["state"] == "waiting_user" and n["pending_party"] == n["my_role"]
            if not n["waiting_for_me"]:
                n["pending_question"] = None
                if n["state"] == "waiting_user":
                    n["agreed_price"] = None  # a price proposed to the other side, not agreed yet
            n["messages"] = db.rows(
                conn,
                "SELECT round, sender, price, text FROM messages WHERE negotiation_id = ? ORDER BY id",
                n["id"],
            )
    return {"listings": listings, "wants": wants, "negotiations": negs}


@mcp.tool()
def answer_agent(
    user: str,
    negotiation_id: int,
    decision: Literal["accept", "reject", "reply"],
    reply_text: str = "",
) -> dict:
    """Answer a question the user's agent asked (shown by check_status as waiting_for_me).

    decision: "accept" agrees to the proposed price, "reject" ends the negotiation,
    "reply" passes `reply_text` back to the agent (e.g. an answer about the item) and continues.
    """
    with db.connect() as conn:
        u = _require_user(conn, user)
        p = engine.parties(conn, negotiation_id)
        if p is None:
            raise ToolError("找不到這筆議價。")
        role = "seller" if p["seller_name"] == u["name"] else "buyer" if p["buyer_name"] == u["name"] else None
        if role is None:
            raise ToolError("這筆議價不屬於這位使用者。")
        if p["state"] != "waiting_user" or p["pending_party"] != role:
            raise ToolError("這筆議價目前沒有在等您回答。")

        if decision == "accept":
            if p["agreed_price"] is None:
                raise ToolError("這個問題沒有提議價格，請用 reply 回答。")
            engine.mark_agreed(conn, negotiation_id, p["agreed_price"])
            restart = False
        elif decision == "reject":
            engine.mark_failed(conn, negotiation_id, f"{'賣家' if role == 'seller' else '買家'}拒絕")
            restart = False
        else:
            db.add_message(conn, negotiation_id, p["round"], f"{role}_agent", f"（本人回覆）{reply_text}")
            db.set_negotiation(conn, negotiation_id, state="negotiating", pending_party=None,
                               pending_question=None, agreed_price=None)
            restart = True
        state = engine.parties(conn, negotiation_id)["state"]
    if restart:
        engine.start_negotiation_async(negotiation_id)
    return {"negotiation_id": negotiation_id, "state": state}


@mcp.tool()
def confirm_deal(user: str, negotiation_id: int) -> dict:
    """Confirm an agreed deal. The deal closes only after both buyer and seller confirm."""
    with db.connect() as conn:
        u = _require_user(conn, user)
        p = engine.parties(conn, negotiation_id)
        if p is None:
            raise ToolError("找不到這筆議價。")
        if p["state"] != "agreed":
            raise ToolError(f"這筆議價目前是「{p['state']}」，還不能確認成交。")
        if p["seller_name"] == u["name"]:
            db.set_negotiation(conn, negotiation_id, seller_confirmed=1)
        elif p["buyer_name"] == u["name"]:
            db.set_negotiation(conn, negotiation_id, buyer_confirmed=1)
        else:
            raise ToolError("這筆議價不屬於這位使用者。")
        p = engine.parties(conn, negotiation_id)
        if p["seller_confirmed"] and p["buyer_confirmed"]:
            engine.mark_closed(conn, negotiation_id)
        p = engine.parties(conn, negotiation_id)
    return {
        "negotiation_id": negotiation_id,
        "state": p["state"],
        "agreed_price": p["agreed_price"],
        "seller_confirmed": bool(p["seller_confirmed"]),
        "buyer_confirmed": bool(p["buyer_confirmed"]),
    }


def main() -> None:
    db.init_db()
    port = int(os.environ.get("PORT", "8000"))
    # Claude reaches us through the ngrok domain, so that Host must be allowed
    # alongside localhost (the SDK's DNS-rebinding protection rejects anything else).
    public_host = os.environ.get("PUBLIC_HOST", "").strip()
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"] + ([public_host] if public_host else [])
    mcp.run(
        "streamable-http",
        host="127.0.0.1",
        port=port,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=["https://claude.ai", "http://localhost:*", "http://127.0.0.1:*"]
            + ([f"https://{public_host}"] if public_host else []),
        ),
    )
