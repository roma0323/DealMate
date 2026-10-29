"""Matching, buyer questions, deals and meetup scheduling.

Rules only: the backend never calls an AI. The user's own Claude does the thinking
(reading the photo, turning a wish into conditions, reading the calendar) and sends
structured data; everything here is plain code.

Deal states: requested -> confirmed -> scheduled, or cancelled from any of them.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, photos
from .notify import send_email_async

log = logging.getLogger("dealmate.engine")

TZ = ZoneInfo("Asia/Taipei")
MEETUP_MINUTES = 60       # length of the calendar invite, and the shortest usable overlap
SEARCH_DAYS = 14          # how far ahead to look for a meetup time

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_GROUPS = {"weekday": DAYS[:5], "weekend": DAYS[5:], "daily": DAYS}
OPS = ("<=", ">=", "<", ">", "=", "!=", "contains")

STATUS_HINT = "回到 Claude 輸入「DealMate 進度如何？」"


# ---------- validating what Claude sends ----------

def check_slots(slots: list[dict]) -> list[dict]:
    """Validate meetup slots. Each is {"day": "mon".."sun"|"weekday"|"weekend"|"daily"} or
    {"date": "YYYY-MM-DD"}, plus "start" and "end" as "HH:MM". Raises ValueError."""
    out = []
    for s in slots:
        day, on = s.get("day"), s.get("date")
        if (day is None) == (on is None):
            raise ValueError(f"時段 {s} 要有 day 或 date 其中一個。")
        if day is not None and day not in DAYS and day not in DAY_GROUPS:
            raise ValueError(f"day 只能是 {', '.join(DAYS + list(DAY_GROUPS))}，收到「{day}」。")
        try:
            if on is not None:
                date.fromisoformat(on)
            start, end = _hm(s["start"]), _hm(s["end"])
        except (KeyError, ValueError):
            raise ValueError(f"時段 {s} 的格式不對：date 用 YYYY-MM-DD，start／end 用 HH:MM。")
        if end <= start:
            raise ValueError(f"時段 {s} 的結束時間要晚於開始時間。")
        out.append({k: v for k, v in s.items() if k in ("day", "date", "start", "end")})
    return out


def check_conditions(conditions: list[dict]) -> list[dict]:
    for c in conditions:
        if not c.get("key") or c.get("op") not in OPS or "value" not in c:
            raise ValueError(f"條件 {c} 需要 key、op（{' '.join(OPS)}）和 value。")
    return [{"key": c["key"], "op": c["op"], "value": c["value"]} for c in conditions]


def check_specs(specs: list[dict]) -> list[dict]:
    for s in specs:
        if not s.get("key") or "value" not in s:
            raise ValueError(f"規格 {s} 需要 key 和 value。")
        if s.get("source") not in ("official", "seller", "agent"):
            raise ValueError(f"規格「{s['key']}」的 source 要是 official、seller 或 agent。")
    return [{k: s[k] for k in ("key", "label", "value", "unit", "source") if k in s} for s in specs]


def _hm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(text)
    return h, m


# ---------- matching ----------

def _number(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "1", "是", "可", "有"):
        return True
    if s in ("false", "no", "0", "否", "不可", "沒有", "無"):
        return False
    return None


def _holds(spec, op: str, want) -> bool:
    if op == "contains":
        return str(want).lower() in str(spec).lower()
    a, b = _number(spec), _number(want)
    if a is not None and b is not None:
        return {"<=": a <= b, ">=": a >= b, "<": a < b, ">": a > b, "=": a == b, "!=": a != b}[op]
    if op in ("=", "!="):
        if isinstance(want, bool) or isinstance(spec, bool):
            same = _as_bool(spec) == _as_bool(want)
        else:
            same = str(spec).strip().lower() == str(want).strip().lower()
        return same if op == "=" else not same
    return False  # <, > on text: can't hold


def evaluate(listing: dict, want: dict) -> dict | None:
    """Does the listing meet the want? None if not; otherwise which conditions are still unknown
    (the listing doesn't state that spec, so the buyer's Claude should look it up or ask)."""
    if listing["category"] != want["category"]:
        return None
    if not (want["budget_min"] <= listing["price"] <= want["budget_max"]):
        return None
    specs = {s["key"]: s["value"] for s in listing["specs"]}
    unknown = []
    for c in want["conditions"]:
        if c["key"] not in specs or specs[c["key"]] in (None, ""):
            unknown.append(c)
        elif not _holds(specs[c["key"]], c["op"], c["value"]):
            return None
    return {"unknown_conditions": unknown}


def public_listing(conn, listing_id: int) -> dict | None:
    """What anyone may see about a listing: no seller email, photo path or upload token."""
    l = db.row(
        conn,
        "SELECT l.id, l.title, l.category, l.description, l.condition, l.location, l.price, l.specs, "
        "l.size, l.needs_disassembly, l.needs_truck, l.status, l.created_at, l.photo_path, "
        "s.name AS seller, s.stations AS seller_stations "
        "FROM listings l JOIN users s ON s.id = l.seller_id WHERE l.id = ?",
        listing_id,
    )
    if l is None:
        return None
    l["has_photo"] = photos.photo_file(l) is not None
    del l["photo_path"]
    l["needs_disassembly"], l["needs_truck"] = bool(l["needs_disassembly"]), bool(l["needs_truck"])
    l["qa"] = db.rows(
        conn,
        "SELECT question, answer FROM questions WHERE listing_id = ? AND public = 1 AND status = 'answered' "
        "ORDER BY id",
        listing_id,
    )
    return l


def matches_for_want(conn, want_id: int) -> list[dict]:
    """Every open listing that meets the want, as public listings; remembers them so the
    buyer isn't emailed about the same items later."""
    want = db.row(conn, "SELECT * FROM wants WHERE id = ?", want_id)
    candidates = db.rows(
        conn,
        "SELECT * FROM listings WHERE status = 'open' AND category = ? AND seller_id != ? ORDER BY price, id",
        want["category"], want["buyer_id"],
    )
    found = []
    for listing in candidates:
        m = evaluate(listing, want)
        if m is None:
            continue
        conn.execute("INSERT OR IGNORE INTO want_matches (want_id, listing_id) VALUES (?, ?)",
                     (want_id, listing["id"]))
        found.append({**public_listing(conn, listing["id"]), **m})
    return found


def notify_buyers_for_listing(conn, listing_id: int) -> int:
    """Email every buyer whose open want this listing meets (once per want). Returns how many."""
    listing = db.row(conn, "SELECT * FROM listings WHERE id = ?", listing_id)
    if listing["status"] != "open":
        return 0
    wants = db.rows(
        conn,
        """
        SELECT w.*, b.email AS buyer_email FROM wants w JOIN users b ON b.id = w.buyer_id
        WHERE w.status = 'open' AND w.category = ? AND w.buyer_id != ?
          AND NOT EXISTS (SELECT 1 FROM want_matches m WHERE m.want_id = w.id AND m.listing_id = ?)
        """,
        listing["category"], listing["seller_id"], listing_id,
    )
    notified = 0
    for want in wants:
        m = evaluate(listing, want)
        if m is None:
            continue
        conn.execute("INSERT INTO want_matches (want_id, listing_id) VALUES (?, ?)", (want["id"], listing_id))
        unknown = "、".join(c["key"] for c in m["unknown_conditions"])
        send_email_async(
            want["buyer_email"],
            f"DealMate：有符合「{want['description']}」的商品",
            f"符合您需求的商品上架了：「{listing['title']}」，售價 {listing['price']} 元。\n"
            f"狀況：{listing['condition'] or '未提供'}\n"
            f"地點：{listing['location'] or '未提供'}\n"
            + (f"商品介紹還沒寫到：{unknown}，可以請 Claude 幫您問賣家。\n" if unknown else "")
            + f"\n想看詳細資料、問問題或決定購買，請{STATUS_HINT}",
            image=photos.photo_file(listing),
        )
        notified += 1
    return notified


# ---------- buyer questions ----------

def ask_seller(conn, listing: dict, buyer: dict, question: str) -> int:
    cur = conn.execute(
        "INSERT INTO questions (listing_id, buyer_id, question) VALUES (?, ?, ?)",
        (listing["id"], buyer["id"], question),
    )
    seller = db.row(conn, "SELECT * FROM users WHERE id = ?", listing["seller_id"])
    send_email_async(
        seller["email"],
        f"DealMate：買家對「{listing['title']}」有問題",
        f"有買家問：{question}\n\n請{STATUS_HINT}回答。"
        "回答時可以選擇公開，答案會寫進商品介紹，之後的買家就不用再問。",
    )
    return cur.lastrowid


def answer_question(conn, q: dict, answer: str, public: bool) -> None:
    conn.execute(
        "UPDATE questions SET answer = ?, public = ?, status = 'answered', answered_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (answer, int(public), q["id"]),
    )
    p = db.row(
        conn,
        "SELECT l.title, b.email FROM listings l, users b WHERE l.id = ? AND b.id = ?",
        q["listing_id"], q["buyer_id"],
    )
    send_email_async(
        p["email"],
        f"DealMate：賣家回答了「{p['title']}」的問題",
        f"您問：{q['question']}\n賣家回答：{answer}\n\n想購買或繼續問，請{STATUS_HINT}",
    )


# ---------- deals ----------

def deal_parties(conn, deal_id: int) -> dict | None:
    """Everything about one deal, joined. Internal use: contains both emails."""
    return db.row(
        conn,
        """
        SELECT d.*, l.title, l.seller_id, l.size, l.needs_disassembly, l.needs_truck,
               s.name AS seller_name, s.email AS seller_email, s.slots AS seller_slots, s.stations AS seller_stations,
               b.name AS buyer_name, b.email AS buyer_email, b.slots AS buyer_slots, b.stations AS buyer_stations
        FROM deals d
        JOIN listings l ON l.id = d.listing_id
        JOIN users s ON s.id = l.seller_id
        JOIN users b ON b.id = d.buyer_id
        WHERE d.id = ?
        """,
        deal_id,
    )


def request_purchase(conn, listing: dict, buyer: dict) -> int:
    """The buyer decided to buy at the listed price. Reserves the item and asks the seller."""
    cur = conn.execute(
        "INSERT INTO deals (listing_id, buyer_id, price, buyer_confirmed) VALUES (?, ?, ?, 1)",
        (listing["id"], buyer["id"], listing["price"]),
    )
    conn.execute("UPDATE listings SET status = 'reserved' WHERE id = ?", (listing["id"],))
    seller = db.row(conn, "SELECT * FROM users WHERE id = ?", listing["seller_id"])
    send_email_async(
        seller["email"],
        f"DealMate：有人要買「{listing['title']}」",
        f"{buyer['name']} 想以售價 {listing['price']} 元購買「{listing['title']}」。\n"
        f"請{STATUS_HINT}確認成交或婉拒。確認後 DealMate 會比對雙方時間地點，寄出面交通知。",
    )
    return cur.lastrowid


def confirm(conn, deal_id: int) -> None:
    """The seller agreed: the item is sold, and the meetup gets scheduled."""
    d = deal_parties(conn, deal_id)
    db.set_deal(conn, deal_id, seller_confirmed=1, state="confirmed")
    conn.execute("UPDATE listings SET status = 'sold' WHERE id = ?", (d["listing_id"],))
    # The buyer's wants that this item answered are done.
    conn.execute(
        "UPDATE wants SET status = 'closed' WHERE buyer_id = ? AND status = 'open' "
        "AND id IN (SELECT want_id FROM want_matches WHERE listing_id = ?)",
        (d["buyer_id"], d["listing_id"]),
    )
    schedule(conn, deal_id)


def cancel(conn, deal_id: int, by: str) -> None:
    """`by` is 'seller' or 'buyer'. Puts the item back on sale."""
    d = deal_parties(conn, deal_id)
    db.set_deal(conn, deal_id, state="cancelled")
    conn.execute("UPDATE listings SET status = 'open' WHERE id = ?", (d["listing_id"],))
    other = "buyer" if by == "seller" else "seller"
    who = "賣家" if by == "seller" else "買家"
    send_email_async(
        d[f"{other}_email"],
        f"DealMate：「{d['title']}」的交易已取消",
        f"{who}取消了「{d['title']}」的交易。" + ("商品已重新上架。" if other == "seller" else ""),
    )


def schedule(conn, deal_id: int, after: datetime | None = None) -> datetime | None:
    """Pick the earliest meetup both can make (after `after`, default now) and email both
    with a calendar invite. Without one, asks both to update their times or stations."""
    d = deal_parties(conn, deal_id)
    found = find_meetup(d["seller_slots"], d["buyer_slots"], d["seller_stations"], d["buyer_stations"],
                        after or datetime.now(TZ))
    if found is None:
        db.set_deal(conn, deal_id, state="confirmed", meetup_at=None, meetup_station=None)
        body = (
            f"「{d['title']}」已確認以 {d['price']} 元成交，但 DealMate 找不到未來 {SEARCH_DAYS} 天內"
            "雙方都可以的面交時段和捷運站。\n"
            f"請{STATUS_HINT}，告訴 Claude 其他可以面交的時間或地點。"
        )
        send_email_async(d["seller_email"], f"DealMate：「{d['title']}」需要另約面交", body)
        send_email_async(d["buyer_email"], f"DealMate：「{d['title']}」需要另約面交", body)
        return None

    at, station = found
    db.set_deal(conn, deal_id, state="scheduled", meetup_at=at.strftime("%Y-%m-%dT%H:%M"), meetup_station=station)
    when = f"{at.month}/{at.day}（{'一二三四五六日'[at.weekday()]}）{at:%H:%M}"
    moving = "、".join(x for x, on in (("需要拆裝", d["needs_disassembly"]), ("需要貨車", d["needs_truck"])) if on)
    body = (
        f"「{d['title']}」以 {d['price']} 元成交。\n"
        f"面交：{when}，{station}站\n"
        f"賣家：{d['seller_name']}（{d['seller_email']}）\n買家：{d['buyer_name']}（{d['buyer_email']}）\n"
        + (f"尺寸：{d['size']}\n" if d["size"] else "")
        + (f"搬運：{moving}\n" if moving else "")
        + f"\n附件是行事曆邀請，點一下就能加入。時間不行的話，請{STATUS_HINT}要求更換。"
    )
    event = {
        "uid": f"deal-{deal_id}@dealmate",
        "start": at,
        "minutes": MEETUP_MINUTES,
        "summary": f"DealMate 面交：{d['title']}",
        "location": f"{station}站",
        "description": body,
    }
    for side in ("seller", "buyer"):
        send_email_async(d[f"{side}_email"], f"DealMate：「{d['title']}」面交時間 {when}", body, event=event)
    return at


# ---------- meetup time and place ----------

def _norm_station(s: str) -> str:
    s = s.strip().replace(" ", "")
    return s[:-1] if s.endswith("站") and len(s) > 1 else s


def common_station(seller: list[str], buyer: list[str]) -> str | None:
    """First station of the seller's that the buyer also listed; if only one side listed
    stations, the other side is taken to be flexible."""
    if seller and buyer:
        theirs = {_norm_station(s) for s in buyer}
        return next((_norm_station(s) for s in seller if _norm_station(s) in theirs), None)
    if seller or buyer:
        return _norm_station((seller or buyer)[0])
    return None


def _windows(slots: list[dict], day: date) -> list[tuple[datetime, datetime]]:
    out = []
    for s in slots:
        if "date" in s:
            if s["date"] != day.isoformat():
                continue
        elif DAYS[day.weekday()] not in DAY_GROUPS.get(s["day"], [s["day"]]):
            continue
        (sh, sm), (eh, em) = _hm(s["start"]), _hm(s["end"])
        start = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(hours=sh, minutes=sm)
        end = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(hours=eh, minutes=em)
        out.append((start, end))
    return out


def find_meetup(seller_slots, buyer_slots, seller_stations, buyer_stations,
                after: datetime) -> tuple[datetime, str] | None:
    """Earliest start after `after` (on the half hour) when both are free for MEETUP_MINUTES."""
    station = common_station(seller_stations, buyer_stations)
    if station is None:
        return None
    after = after.astimezone(TZ)
    # Round up to the next half hour.
    after = after.replace(second=0, microsecond=0) + timedelta(minutes=(-after.minute) % 30 or 30)
    need = timedelta(minutes=MEETUP_MINUTES)
    # Look SEARCH_DAYS ahead, or further if a slot names a later date.
    dated = [date.fromisoformat(s["date"]) for s in seller_slots + buyer_slots if "date" in s]
    last = max([after.date() + timedelta(days=SEARCH_DAYS), *dated])
    for i in range((last - after.date()).days + 1):
        day = after.date() + timedelta(days=i)
        best = None
        for s_start, s_end in _windows(seller_slots, day):
            for b_start, b_end in _windows(buyer_slots, day):
                start, end = max(s_start, b_start, after), min(s_end, b_end)
                if end - start >= need and (best is None or start < best):
                    best = start
        if best is not None:
            return best, station
    return None
