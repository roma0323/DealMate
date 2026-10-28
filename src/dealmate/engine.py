"""Matching, the pricing rules, and the negotiation state machine.

States: matched -> negotiating -> (waiting_user <-> negotiating) -> agreed -> closed
        any active state -> failed

Prices are always decided here, in code. The LLM (llm.py) only puts each offer
into words and writes the reports, and never sees the other side's limit.
"""

import logging
import os
import threading
import time

from . import db, llm
from .notify import send_email_async

log = logging.getLogger("dealmate.engine")

MAX_ROUNDS = 6
CONCESSION_STEPS = 4         # each side reaches its limit after this many concessions
BUYER_OPENING_RATIO = 0.8    # buyer opens at 80% of budget
ROUND_DELAY = float(os.environ.get("DEALMATE_ROUND_DELAY", "1"))  # seconds between rounds


# ---------- matching ----------

def find_match_for_listing(conn, listing_id: int) -> int | None:
    listing = db.row(conn, "SELECT * FROM listings WHERE id = ?", listing_id)
    want = db.row(
        conn,
        "SELECT * FROM wants WHERE status = 'open' AND lower(category) = lower(?) "
        "AND buyer_id != ? ORDER BY created_at LIMIT 1",
        listing["category"], listing["seller_id"],
    )
    return _open_negotiation(conn, listing, want) if want else None


def find_match_for_want(conn, want_id: int) -> int | None:
    want = db.row(conn, "SELECT * FROM wants WHERE id = ?", want_id)
    listing = db.row(
        conn,
        "SELECT * FROM listings WHERE status = 'open' AND lower(category) = lower(?) "
        "AND seller_id != ? ORDER BY created_at LIMIT 1",
        want["category"], want["buyer_id"],
    )
    return _open_negotiation(conn, listing, want) if listing else None


def _open_negotiation(conn, listing: dict, want: dict) -> int:
    cur = conn.execute(
        "INSERT INTO negotiations (listing_id, want_id) VALUES (?, ?)", (listing["id"], want["id"])
    )
    neg_id = cur.lastrowid
    conn.execute("UPDATE listings SET status = 'matched' WHERE id = ?", (listing["id"],))
    conn.execute("UPDATE wants SET status = 'matched' WHERE id = ?", (want["id"],))
    db.add_message(conn, neg_id, 0, "system", f"配對成功：{listing['title']} ↔ {want['description']}")
    return neg_id


def parties(conn, negotiation_id: int) -> dict:
    """Everything about one negotiation, joined. Internal use only: contains floor and budget."""
    return db.row(
        conn,
        """
        SELECT n.*, l.title, l.condition, l.location AS listing_location, l.ask_price,
               l.floor_price, l.auto_accept_price, w.description AS want_description, w.budget,
               s.name AS seller_name, s.email AS seller_email,
               b.name AS buyer_name, b.email AS buyer_email
        FROM negotiations n
        JOIN listings l ON l.id = n.listing_id
        JOIN wants w ON w.id = n.want_id
        JOIN users s ON s.id = l.seller_id
        JOIN users b ON b.id = w.buyer_id
        WHERE n.id = ?
        """,
        negotiation_id,
    )


def notify_both(p: dict, subject: str, body: str) -> None:
    send_email_async(p["seller_email"], subject, body)
    send_email_async(p["buyer_email"], subject, body)


# ---------- pricing rules (code, not AI) ----------

def _round_half_up(x: float) -> int:
    return int(x + 0.5)


def seller_offer(p: dict, round_: int) -> float:
    """Start at the asking price, concede (ask - floor) / 4 per round, never below the floor."""
    step = (p["ask_price"] - p["floor_price"]) / CONCESSION_STEPS
    return max(p["floor_price"], p["ask_price"] - step * (round_ - 1))


def buyer_offer(p: dict, round_: int) -> float:
    """Start at 80% of the budget, raise (budget - opening) / 4 per round, never above the budget."""
    opening = p["budget"] * BUYER_OPENING_RATIO
    step = (p["budget"] - opening) / CONCESSION_STEPS
    return min(p["budget"], opening + step * (round_ - 1))


def deal_price(p: dict, seller: float, buyer: float) -> int:
    """Midpoint of the two offers, rounded to 10, kept inside floor..budget."""
    price = _round_half_up((seller + buyer) / 2 / 10) * 10
    return max(p["floor_price"], min(p["budget"], price))


# ---------- negotiation loop ----------

def start_negotiation_async(negotiation_id: int) -> None:
    """Run the negotiation in the background so the MCP tool call returns right away."""
    threading.Thread(target=_safe_run, args=(negotiation_id,), daemon=True).start()


def _safe_run(negotiation_id: int) -> None:
    try:
        run_negotiation(negotiation_id)
    except Exception:
        log.exception("negotiation %s crashed", negotiation_id)


def run_negotiation(negotiation_id: int) -> None:
    with db.connect() as conn:
        p = parties(conn, negotiation_id)
        if p["state"] not in ("matched", "negotiating"):
            return
        just_matched = p["state"] == "matched"
        db.set_negotiation(conn, negotiation_id, state="negotiating")
        last_buyer_text = (db.row(
            conn,
            "SELECT text FROM messages WHERE negotiation_id = ? AND sender = 'buyer_agent' ORDER BY id DESC LIMIT 1",
            negotiation_id,
        ) or {}).get("text")

    # Only on the first run: a run resumed after answer_agent doesn't repeat them.
    if just_matched:
        notify_both(
            p,
            f"DealMate：「{p['title']}」配對成功",
            f"您的「{p['title']}」找到了交易對象。\n"
            f"賣家地點：{p['listing_location'] or '未提供'}\n"
            "您的代理人接下來會開始議價。",
        )
        notify_both(
            p,
            f"DealMate：「{p['title']}」議價中",
            f"您的代理人正在為「{p['title']}」議價。有結果或需要您決定時會再通知您，"
            "也可以隨時回到 Claude 問「DealMate 進度如何」。",
        )

    item = {"title": p["title"], "condition": p["condition"]}
    for round_ in range(p["round"] + 1, MAX_ROUNDS + 1):
        if round_ > 1:
            time.sleep(ROUND_DELAY)
        s_offer, b_offer = seller_offer(p, round_), buyer_offer(p, round_)
        s_shown, b_shown = _round_half_up(s_offer), _round_half_up(b_offer)

        # Wording happens outside the DB transaction: with an API key it is a network call.
        # Each side's wording only gets its own offer and the other side's public message.
        s_holding = round_ > 1 and s_shown == _round_half_up(seller_offer(p, round_ - 1))
        b_holding = round_ > 1 and b_shown == _round_half_up(buyer_offer(p, round_ - 1))
        seller_text = llm.offer_message("seller", s_shown, item, last_buyer_text, round_, s_holding)
        buyer_text = llm.offer_message("buyer", b_shown, item, seller_text, round_, b_holding)
        last_buyer_text = buyer_text

        with db.connect() as conn:
            db.add_message(conn, negotiation_id, round_, "seller_agent", seller_text, s_shown)
            db.add_message(conn, negotiation_id, round_, "buyer_agent", buyer_text, b_shown)
            db.set_negotiation(conn, negotiation_id, round=round_, seller_offer=s_shown, buyer_offer=b_shown)

            if b_offer >= s_offer:
                price = deal_price(p, s_offer, b_offer)
                if price >= p["auto_accept_price"]:
                    mark_agreed(conn, negotiation_id, price)
                else:
                    ask_user(
                        conn, negotiation_id, "seller",
                        f"買家出價 {b_shown} 元，我們這輪喊 {s_shown} 元，雙方已有交集。"
                        f"建議以 {price} 元成交，但低於您設定的自動接受價 {p['auto_accept_price']} 元，"
                        "要接受嗎？",
                        proposed_price=price,
                    )
                return

        log.info("negotiation %s round %s: seller %s, buyer %s", negotiation_id, round_, s_shown, b_shown)

    with db.connect() as conn:
        mark_failed(conn, negotiation_id, f"{MAX_ROUNDS} 輪內價格沒有交集")


def ask_user(conn, negotiation_id: int, party: str, question: str, proposed_price: int | None = None) -> None:
    """Pause the negotiation until `party` ('seller' or 'buyer') answers via answer_agent.

    With proposed_price, accepting the answer agrees the deal at that price.
    """
    db.set_negotiation(conn, negotiation_id, state="waiting_user", pending_party=party,
                       pending_question=question, agreed_price=proposed_price)
    p = parties(conn, negotiation_id)
    send_email_async(
        p[f"{party}_email"],
        f"DealMate：「{p['title']}」需要您決定",
        f"{question}\n\n請回到 Claude 回覆您的 DealMate 代理人。",
    )


def mark_agreed(conn, negotiation_id: int, price: int) -> None:
    p = parties(conn, negotiation_id)
    history = db.rows(
        conn, "SELECT sender, price FROM messages WHERE negotiation_id = ? AND price IS NOT NULL ORDER BY id",
        negotiation_id,
    )
    seller_prices = [m["price"] for m in history if m["sender"] == "seller_agent"]
    buyer_prices = [m["price"] for m in history if m["sender"] == "buyer_agent"]
    facts = {"title": p["title"], "price": price, "rounds": p["round"],
             "seller_prices": seller_prices, "buyer_prices": buyer_prices}
    # Each report only includes its own side's private limit.
    seller_report = llm.deal_report("seller", {**facts, "floor_price": p["floor_price"]})
    buyer_report = llm.deal_report("buyer", {**facts, "budget": p["budget"]})

    db.set_negotiation(conn, negotiation_id, state="agreed", agreed_price=price, pending_party=None,
                       pending_question=None, seller_report=seller_report, buyer_report=buyer_report)
    db.add_message(conn, negotiation_id, p["round"], "system", f"價格談成：{price} 元，等待雙方確認。", price)
    for side, report in (("seller", seller_report), ("buyer", buyer_report)):
        send_email_async(
            p[f"{side}_email"],
            f"DealMate：「{p['title']}」談成 {price} 元",
            f"{report}\n\n請回到 Claude 確認成交，雙方都確認後才算成交。",
        )


def mark_failed(conn, negotiation_id: int, reason: str) -> None:
    db.set_negotiation(conn, negotiation_id, state="failed", pending_party=None, pending_question=None)
    p = parties(conn, negotiation_id)
    db.add_message(conn, negotiation_id, p["round"], "system", f"議價結束，未成交：{reason}")
    # Put both sides back on the market.
    conn.execute("UPDATE listings SET status = 'open' WHERE id = ?", (p["listing_id"],))
    conn.execute("UPDATE wants SET status = 'open' WHERE id = ?", (p["want_id"],))
    notify_both(p, f"DealMate：「{p['title']}」未成交", f"這次議價沒有成交（{reason}）。")


def mark_closed(conn, negotiation_id: int) -> None:
    db.set_negotiation(conn, negotiation_id, state="closed")
    p = parties(conn, negotiation_id)
    conn.execute("UPDATE listings SET status = 'sold' WHERE id = ?", (p["listing_id"],))
    conn.execute("UPDATE wants SET status = 'bought' WHERE id = ?", (p["want_id"],))
    db.add_message(conn, negotiation_id, p["round"], "system", f"成交：{p['agreed_price']} 元。")
    notify_both(
        p,
        f"DealMate：「{p['title']}」成交",
        f"「{p['title']}」以 {p['agreed_price']} 元成交。\n"
        f"賣家：{p['seller_name']}（{p['seller_email']}）\n買家：{p['buyer_name']}（{p['buyer_email']}）\n"
        "請直接聯絡對方約面交或寄送。",
    )
