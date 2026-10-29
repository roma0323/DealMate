"""DealMate MCP server: the tools Claude calls through the custom connector."""

import base64
import binascii
import logging
import os
from datetime import datetime, timedelta
from typing import Literal, NotRequired, TypedDict

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings

from . import checklists, db, engine, photos, ui
from .ui import ui_meta

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

mcp = MCPServer(
    name="DealMate",
    instructions=(
        "DealMate is a second-hand trading agent: it fills in listing details, finds items that fit, "
        "asks the right questions and arranges the meetup. Prices are fixed: there is no bargaining. "
        "You (the user's own AI) do the thinking; DealMate only stores data, matches by rules and sends email. "
        "Always call register_user first with the user's name and email if they have not registered, and "
        "pass the same `user` name on every later call. Reply to the user in Traditional Chinese.\n"
        "Selling: call get_category_guide, identify the item from the photo and look up its specs "
        "(dimensions, model, new price), mark each spec's source, show the draft to the seller and call "
        "create_listing only after they confirm it. For large items ask for the size and whether it needs "
        "disassembly or a truck. Ask when and at which MRT stations they can meet up.\n"
        "Buying: call get_category_guide, ask what you need (e.g. the height under the counter), turn the "
        "wish into a budget range and conditions with the guide's keys, then call create_want. Remind the "
        "buyer of the guide's checklist; answer from the listing when it says so, otherwise use ask_seller. "
        "When the buyer decides, call request_purchase.\n"
        "Sellers add a photo with the upload button on the listing card that create_listing shows."
    ),
    extensions=[ui.apps],
)


class Slot(TypedDict):
    """When a user can meet up: a weekday (or weekday/weekend/daily) or one date, with a time range."""

    day: NotRequired[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun", "weekday", "weekend", "daily"]]
    date: NotRequired[str]  # YYYY-MM-DD, e.g. a free evening read from the user's calendar
    start: str              # HH:MM
    end: str                # HH:MM


class Spec(TypedDict):
    key: str                # a field key from get_category_guide, e.g. "height_cm"
    value: str | float | bool
    label: NotRequired[str]
    unit: NotRequired[str]
    source: Literal["official", "seller", "agent"]  # official spec / the seller said so / your guess


class Condition(TypedDict):
    key: str                # same keys as listing specs, from get_category_guide
    op: Literal["<=", ">=", "<", ">", "=", "!=", "contains"]
    value: str | float | bool


def _require_user(conn, user: str) -> dict:
    u = db.get_user(conn, user)
    if u is None:
        raise ToolError(f"找不到使用者「{user}」，請先呼叫 register_user。")
    return u


def _set_availability(conn, u: dict, slots: list[Slot] | None, stations: list[str] | None) -> None:
    try:
        db.set_availability(conn, u["id"], engine.check_slots(slots) if slots is not None else None, stations)
    except ValueError as e:
        raise ToolError(str(e))


def _checked(check, value):
    try:
        return check(value)
    except ValueError as e:
        raise ToolError(str(e))


@mcp.tool()
def register_user(
    name: str, email: str, meetup_slots: list[Slot] | None = None, meetup_stations: list[str] | None = None
) -> dict:
    """Register a DealMate user, or update their email if the name already exists.

    Call this before anything else. Ask the user for a display name and an email
    address; DealMate emails them when something happens while they are not in the chat.
    meetup_slots / meetup_stations: when and at which MRT stations they can usually meet up,
    e.g. [{"day": "weekday", "start": "18:00", "end": "22:00"}] and ["南港", "汐止"].
    They can also be given later with create_listing, create_want or request_purchase.
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users (name, email) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET email = excluded.email",
            (name, email),
        )
        u = db.get_user(conn, name)
        _set_availability(conn, u, meetup_slots, meetup_stations)
        u = db.get_user(conn, name)
    return {"user_id": u["id"], "name": u["name"], "email": u["email"],
            "meetup_slots": u["slots"], "meetup_stations": u["stations"]}


@mcp.tool()
def get_category_guide(category: str) -> dict:
    """The structured fields and the buyer's checklist for a category, e.g. "冰箱", "落地燈", "手機".

    Sellers: fill `specs` of create_listing with these field keys. Buyers: write the
    `conditions` of create_want with the same keys, and remind the buyer of `buyer_checklist`.
    `large_item` means: ask about size and whether it needs disassembly or a truck.
    `category` in the result is the name to pass to create_listing / create_want.
    """
    return checklists.guide(category)


@mcp.tool(meta=ui_meta())
def create_listing(
    user: str,
    category: str,
    title: str,
    price: int,
    description: str = "",
    condition: str = "",
    location: str = "",
    specs: list[Spec] | None = None,
    size: str = "",
    needs_disassembly: bool = False,
    needs_truck: bool = False,
    meetup_slots: list[Slot] | None = None,
    meetup_stations: list[str] | None = None,
) -> dict:
    """Seller: put an item up for sale at a fixed price (no bargaining).

    Before calling: call get_category_guide; if the user attached a photo, identify the
    model and look up its specs; fill `specs` with the guide's keys and mark where each came
    from (`source`: official / seller / agent). Write `description` as the listing text,
    saying which parts the seller should double-check. Show the draft to the seller and call
    this only after they confirm. For large items give `size` and the moving needs.
    meetup_slots / meetup_stations update when and where the seller can meet up.

    Buyers whose wants this item meets are emailed. The result is shown as a listing card
    with an upload button, so the seller can add a photo right in the chat.
    """
    specs = checklists.label_specs(category, _checked(engine.check_specs, specs or []))
    with db.connect() as conn:
        seller = _require_user(conn, user)
        _set_availability(conn, seller, meetup_slots, meetup_stations)
        cur = conn.execute(
            "INSERT INTO listings (seller_id, category, title, description, condition, location, price, specs, "
            "size, needs_disassembly, needs_truck) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seller["id"], checklists.canonical(category), title, description, condition, location, price,
             db.dumps(specs), size, int(needs_disassembly), int(needs_truck)),
        )
        listing_id = cur.lastrowid
        token = photos.ensure_upload_token(conn, listing_id)
        notified = engine.notify_buyers_for_listing(conn, listing_id)
        listing = engine.public_listing(conn, listing_id)
    return {
        "view": "listing",
        "headline": f"上架成功，已通知 {notified} 位符合的買家" if notified else "上架成功，目前無匹配",
        "listing_id": listing_id,
        "buyers_notified": notified,
        "listing": listing,
        "upload_token": token,
    }


@mcp.tool(meta=ui_meta())
def update_listing(
    user: str,
    listing_id: int,
    title: str | None = None,
    price: int | None = None,
    description: str | None = None,
    condition: str | None = None,
    location: str | None = None,
    specs: list[Spec] | None = None,
    size: str | None = None,
    needs_disassembly: bool | None = None,
    needs_truck: bool | None = None,
) -> dict:
    """Seller: change their listing. Only the given fields change.

    `specs` are merged by key: a spec with an existing key replaces it, a new key is added.
    Use this e.g. after the seller corrects a spec, or to add a fact they confirmed.
    Buyers whose wants the updated item now meets are emailed.
    """
    new_specs = _checked(engine.check_specs, specs) if specs is not None else None
    with db.connect() as conn:
        seller = _require_user(conn, user)
        l = db.row(conn, "SELECT * FROM listings WHERE id = ? AND seller_id = ?", listing_id, seller["id"])
        if l is None:
            raise ToolError("找不到這位賣家的這件商品。")
        if l["status"] in ("sold", "closed"):
            raise ToolError("這件商品已經結束，不能修改。")
        fields = {k: v for k, v in dict(title=title, price=price, description=description, condition=condition,
                                         location=location, size=size).items() if v is not None}
        for k, v in (("needs_disassembly", needs_disassembly), ("needs_truck", needs_truck)):
            if v is not None:
                fields[k] = int(v)
        if new_specs is not None:
            merged = {s["key"]: s for s in l["specs"]}
            merged.update({s["key"]: s for s in checklists.label_specs(l["category"], new_specs)})
            fields["specs"] = db.dumps(list(merged.values()))
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE listings SET {cols} WHERE id = ?", (*fields.values(), listing_id))
        notified = engine.notify_buyers_for_listing(conn, listing_id)
        listing = engine.public_listing(conn, listing_id)
        token = photos.ensure_upload_token(conn, listing_id)
    return {
        "view": "listing",
        "headline": "已更新" + (f"，已通知 {notified} 位新符合的買家" if notified else ""),
        "listing": listing,
        "buyers_notified": notified,
        "upload_token": token,
    }


@mcp.tool(meta=ui_meta())
def show_listing(user: str, listing_id: int) -> dict:
    """Show one item as a card with its photo, specs (with their sources) and public Q&A.

    Read `specs`, `description` and `qa` before asking the seller anything: if they already
    answer the buyer's question, answer it from there.
    For the item's seller the card also has an upload button for adding or changing the photo.
    """
    with db.connect() as conn:
        u = _require_user(conn, user)
        listing = engine.public_listing(conn, listing_id)
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
    """The listing and its photo bytes. Listings are public, so any registered user may look."""
    with db.connect() as conn:
        _require_user(conn, user)
        listing = db.row(conn, "SELECT * FROM listings WHERE id = ?", listing_id)
    if listing is None:
        raise ToolError("找不到這件商品。")
    path = photos.photo_file(listing)
    if path is None:
        raise ToolError("這件商品還沒有照片。")
    return listing, path.read_bytes()


@mcp.tool()
def view_listing_photo_for_ai(user: str, listing_id: int) -> Image:
    """Look at a listing's photo yourself, e.g. to identify the model and look up its specs,
    or to answer a buyer's question from the photo.

    Returns the image for you (the AI) to see; it is not shown to the user as a file.
    To show the photo to the user, use view_listing_photo_for_human.
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
    """
    listing, data = _viewable_photo(user, listing_id)
    ext, mime = photos.detect_type(data[:16])
    return {
        "content": base64.b64encode(data).decode(),
        "id": f"listing-{listing_id}",
        "mimeType": mime,
        "title": f"{listing['title']}.{ext}",
    }


@mcp.tool(meta=ui_meta())
def create_want(
    user: str,
    category: str,
    description: str,
    budget_max: int,
    budget_min: int = 0,
    conditions: list[Condition] | None = None,
    location: str = "",
    meetup_slots: list[Slot] | None = None,
    meetup_stations: list[str] | None = None,
) -> dict:
    """Buyer: describe something to buy. Matching items are returned now, and the want
    stays open: when a matching item is listed later, the buyer is emailed.

    Before calling: call get_category_guide and turn what the buyer said into conditions
    with its keys. Ask for what you need first, e.g. "要能塞進流理台下" -> ask the height
    (85 cm) -> {"key": "height_cm", "op": "<=", "value": 83} (leaving room for airflow).
    Items are matched by category, price within budget_min..budget_max, and conditions.
    A matching item may still have `unknown_conditions` (specs the listing doesn't state):
    look them up from the model, or ask the seller with ask_seller.
    meetup_slots / meetup_stations update when and where the buyer can meet up.
    The result is shown as product cards.
    """
    if budget_min > budget_max:
        raise ToolError("budget_min 不能大於 budget_max。")
    conditions = _checked(engine.check_conditions, conditions or [])
    with db.connect() as conn:
        buyer = _require_user(conn, user)
        _set_availability(conn, buyer, meetup_slots, meetup_stations)
        cur = conn.execute(
            "INSERT INTO wants (buyer_id, category, description, budget_min, budget_max, conditions, location) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (buyer["id"], checklists.canonical(category), description, budget_min, budget_max,
             db.dumps(conditions), location),
        )
        want_id = cur.lastrowid
        matches = engine.matches_for_want(conn, want_id)
    return {
        "view": "gallery",
        "headline": f"需求已建立，找到 {len(matches)} 件符合的商品" if matches
        else "需求已建立，目前無匹配。有符合的商品上架時會寄 email 通知您",
        "want_id": want_id,
        "count": len(matches),
        "listings": matches,
    }


DEBUG = os.environ.get("DEALMATE_DEBUG", "").strip().lower() in ("1", "true", "yes")


@mcp.tool(meta=ui_meta())
def browse_listings(include_closed: bool = False, category: str = "") -> dict:
    """Browse every item on DealMate with its public details, optionally only one category.
    The result is shown as product cards with photos.

    By default only items still for sale are shown; set include_closed to also show
    reserved, sold and closed ones. (With DEALMATE_DEBUG=1 on the server, the result also
    includes private data for debugging: emails, wants, questions, deals and users.)
    """
    where = [] if include_closed else ["status = 'open'"]
    args = []
    if category:
        where.append("category = ?")
        args.append(checklists.canonical(category))
    sql = "SELECT id FROM listings" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
    with db.connect() as conn:
        listings = [engine.public_listing(conn, r["id"]) for r in db.rows(conn, sql, *args)]
        result = {"view": "gallery", "count": len(listings), "listings": listings}
        if DEBUG:
            for l in listings:
                l["debug"] = {
                    "questions": db.rows(conn, "SELECT id, question, answer, public, status FROM questions "
                                               "WHERE listing_id = ? ORDER BY id", l["id"]),
                    "deals": db.rows(conn, "SELECT d.id, d.state, d.price, d.meetup_at, d.meetup_station, "
                                           "b.name AS buyer FROM deals d JOIN users b ON b.id = d.buyer_id "
                                           "WHERE d.listing_id = ? ORDER BY d.id", l["id"]),
                }
            result["debug"] = True
            result["wants"] = db.rows(
                conn,
                "SELECT w.id, w.category, w.description, w.budget_min, w.budget_max, w.conditions, w.status, "
                "b.name AS buyer FROM wants w JOIN users b ON b.id = w.buyer_id ORDER BY w.id",
            )
            result["users"] = db.rows(conn, "SELECT id, name, email, slots, stations, created_at FROM users ORDER BY id")
    return result


@mcp.tool()
def ask_seller(user: str, listing_id: int, question: str) -> dict:
    """Buyer: ask the seller something the listing can't answer. DealMate emails the seller,
    and emails the buyer when the seller answers.

    Check the listing's specs, description and qa first (show_listing): if they already
    answer it, answer the buyer yourself instead.
    """
    with db.connect() as conn:
        buyer = _require_user(conn, user)
        listing = db.row(conn, "SELECT * FROM listings WHERE id = ?", listing_id)
        if listing is None or listing["status"] in ("sold", "closed"):
            raise ToolError("找不到這件商品，或它已經結束。")
        if listing["seller_id"] == buyer["id"]:
            raise ToolError("這是您自己的商品。")
        question_id = engine.ask_seller(conn, listing, buyer, question)
    return {"question_id": question_id, "status": "pending"}


@mcp.tool()
def answer_question(user: str, question_id: int, answer: str, make_public: bool) -> dict:
    """Seller: answer a buyer's question (check_status lists them under pending_questions).

    Ask the seller whether the answer may be made public. If make_public, it is shown on
    the listing for every buyer, so the next one doesn't need to ask. If the answer is a
    spec (e.g. "不可調光"), also add it with update_listing (source "seller").
    """
    with db.connect() as conn:
        seller = _require_user(conn, user)
        q = db.row(
            conn,
            "SELECT q.* FROM questions q JOIN listings l ON l.id = q.listing_id WHERE q.id = ? AND l.seller_id = ?",
            question_id, seller["id"],
        )
        if q is None:
            raise ToolError("找不到這位賣家要回答的這個問題。")
        engine.answer_question(conn, q, answer, make_public)
    return {"question_id": question_id, "status": "answered", "public": make_public}


@mcp.tool()
def request_purchase(
    user: str, listing_id: int, meetup_slots: list[Slot] | None = None, meetup_stations: list[str] | None = None
) -> dict:
    """Buyer: buy the item at its listed price (there is no bargaining).

    Confirm the item and price with the buyer first. Make sure DealMate knows when and at
    which MRT stations the buyer can meet up (pass them here if not given before). The
    item is reserved and the seller is emailed to confirm; once they do, both get the
    meetup time and place with a calendar invite.
    """
    with db.connect() as conn:
        buyer = _require_user(conn, user)
        listing = db.row(conn, "SELECT * FROM listings WHERE id = ?", listing_id)
        if listing is None:
            raise ToolError("找不到這件商品。")
        if listing["seller_id"] == buyer["id"]:
            raise ToolError("這是您自己的商品。")
        if listing["status"] != "open":
            raise ToolError("這件商品目前已經有人要了，或已經結束。")
        _set_availability(conn, buyer, meetup_slots, meetup_stations)
        deal_id = engine.request_purchase(conn, listing, buyer)
        buyer = db.get_user(conn, user)
    return {
        "deal_id": deal_id,
        "state": "requested",
        "price": listing["price"],
        "meetup_slots": buyer["slots"],
        "meetup_stations": buyer["stations"],
    }


def _deal_role(conn, user: str, deal_id: int) -> tuple[dict, str]:
    u = _require_user(conn, user)
    d = engine.deal_parties(conn, deal_id)
    if d is None:
        raise ToolError("找不到這筆交易。")
    if d["seller_id"] == u["id"]:
        return d, "seller"
    if d["buyer_id"] == u["id"]:
        return d, "buyer"
    raise ToolError("這筆交易不屬於這位使用者。")


@mcp.tool()
def confirm_deal(user: str, deal_id: int, accept: bool = True) -> dict:
    """Seller: confirm (accept=true) or decline (accept=false) a buyer's purchase request.
    Either side may also cancel a deal with accept=false.

    Once the seller confirms, the item is sold and DealMate picks the earliest time and
    MRT station both can make, then emails both with a calendar invite.
    """
    with db.connect() as conn:
        d, role = _deal_role(conn, user, deal_id)
        if d["state"] == "cancelled":
            raise ToolError("這筆交易已經取消。")
        if not accept:
            engine.cancel(conn, deal_id, role)
        elif role == "buyer":
            raise ToolError("買家已經在提出購買時確認了，現在等賣家確認。")
        elif d["state"] != "requested":
            raise ToolError("這筆交易已經確認過了。")
        else:
            engine.confirm(conn, deal_id)
        d = engine.deal_parties(conn, deal_id)
    return {"deal_id": deal_id, "state": d["state"], "price": d["price"],
            "meetup_at": d["meetup_at"], "meetup_station": d["meetup_station"]}


@mcp.tool()
def reschedule_meetup(
    user: str, deal_id: int, meetup_slots: list[Slot] | None = None, meetup_stations: list[str] | None = None
) -> dict:
    """Either side: the proposed meetup doesn't work, or none could be found. Pass the times
    and stations that do work for this user (e.g. free evenings read from their calendar
    as {"date": ...} slots); DealMate picks a new time and emails both a new invite.
    """
    with db.connect() as conn:
        d, role = _deal_role(conn, user, deal_id)
        if d["state"] not in ("confirmed", "scheduled"):
            raise ToolError("這筆交易還沒確認成交，或已經取消。")
        u = db.get_user(conn, user)
        _set_availability(conn, u, meetup_slots, meetup_stations)
        # Without new availability, move to a later day than the current meetup.
        after = None
        if meetup_slots is None and meetup_stations is None and d["meetup_at"]:
            current = datetime.fromisoformat(d["meetup_at"]).replace(tzinfo=engine.TZ)
            after = current.replace(hour=0, minute=0) + timedelta(days=1)
        at = engine.schedule(conn, deal_id, after)
        d = engine.deal_parties(conn, deal_id)
    return {"deal_id": deal_id, "state": d["state"], "meetup_at": d["meetup_at"],
            "meetup_station": d["meetup_station"], "found": at is not None}


@mcp.tool()
def check_status(user: str) -> dict:
    """Show the user's listings, wants, questions and deals, and anything waiting for them:
    `pending_questions` on their listings to answer (answer_question), and deals with
    `waiting_for_me` to confirm (confirm_deal). Call this whenever the user asks how things are going.
    """
    with db.connect() as conn:
        u = _require_user(conn, user)
        listings = [engine.public_listing(conn, r["id"]) for r in
                    db.rows(conn, "SELECT id FROM listings WHERE seller_id = ? ORDER BY id", u["id"])]
        for l in listings:
            l["pending_questions"] = db.rows(
                conn, "SELECT id, question FROM questions WHERE listing_id = ? AND status = 'pending' ORDER BY id",
                l["id"],
            )
        wants = db.rows(
            conn,
            "SELECT id, category, description, budget_min, budget_max, conditions, status FROM wants "
            "WHERE buyer_id = ? ORDER BY id",
            u["id"],
        )
        for w in wants:
            w["matched_listings"] = db.rows(
                conn,
                "SELECT l.id, l.title, l.price, l.status FROM want_matches m JOIN listings l ON l.id = m.listing_id "
                "WHERE m.want_id = ? ORDER BY l.id",
                w["id"],
            )
        my_questions = db.rows(
            conn,
            "SELECT q.id, q.listing_id, l.title, q.question, q.answer, q.status FROM questions q "
            "JOIN listings l ON l.id = q.listing_id WHERE q.buyer_id = ? ORDER BY q.id",
            u["id"],
        )
        deals = db.rows(
            conn,
            """
            SELECT d.id, d.listing_id, l.title, d.price, d.state, d.meetup_at, d.meetup_station,
                   CASE WHEN l.seller_id = ? THEN 'seller' ELSE 'buyer' END AS my_role,
                   CASE WHEN l.seller_id = ? THEN b.name ELSE s.name END AS other_party
            FROM deals d JOIN listings l ON l.id = d.listing_id
            JOIN users s ON s.id = l.seller_id JOIN users b ON b.id = d.buyer_id
            WHERE l.seller_id = ? OR d.buyer_id = ? ORDER BY d.id
            """,
            u["id"], u["id"], u["id"], u["id"],
        )
        for d in deals:
            d["waiting_for_me"] = d["state"] == "requested" and d["my_role"] == "seller"
    return {
        "listings": listings,
        "wants": wants,
        "my_questions": my_questions,
        "deals": deals,
        "meetup_slots": u["slots"],
        "meetup_stations": u["stations"],
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
