"""Matching and the negotiation state machine.

States: matched -> negotiating -> (waiting_user <-> negotiating) -> agreed -> closed
        any active state -> failed

The pricing rules themselves (who offers what each round) are not written yet:
see run_negotiation(). Everything around them -- matching, notifications,
human answers and confirmations -- is in place.
"""

import logging
import threading

from . import db
from .notify import send_email

log = logging.getLogger("dealmate.engine")

MAX_ROUNDS = 6


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
    send_email(p["seller_email"], subject, body)
    send_email(p["buyer_email"], subject, body)


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
        db.set_negotiation(conn, negotiation_id, state="negotiating")

    notify_both(
        p,
        f"DealMate：「{p['title']}」配對成功，開始議價",
        f"您的代理人已開始為「{p['title']}」議價。有結果或需要您決定時會再通知您，"
        "也可以隨時回到 Claude 問「DealMate 進度如何」。",
    )

    # TODO(pricing): the round-by-round offer rules from the design doc go here.
    # Each round must:
    #   1. compute seller_offer / buyer_offer in code (never below floor_price, never above budget)
    #   2. write each offer as a sentence with llm.offer_message() and db.add_message()
    #   3. on overlap -> agreed_price; if it is below auto_accept_price, call ask_user(..., "seller", ...)
    #      otherwise call mark_agreed()
    #   4. after MAX_ROUNDS without overlap -> mark_failed()
    log.info("negotiation %s: pricing rules not implemented yet; staying in 'negotiating'", negotiation_id)


def ask_user(conn, negotiation_id: int, party: str, question: str, proposed_price: int | None = None) -> None:
    """Pause the negotiation until `party` ('seller' or 'buyer') answers via answer_agent.

    With proposed_price, accepting the answer agrees the deal at that price.
    """
    db.set_negotiation(conn, negotiation_id, state="waiting_user", pending_party=party,
                       pending_question=question, agreed_price=proposed_price)
    p = parties(conn, negotiation_id)
    send_email(
        p[f"{party}_email"],
        f"DealMate：「{p['title']}」需要您決定",
        f"{question}\n\n請回到 Claude 回覆您的 DealMate 代理人。",
    )


def mark_agreed(conn, negotiation_id: int, price: int) -> None:
    db.set_negotiation(conn, negotiation_id, state="agreed", agreed_price=price,
                       pending_party=None, pending_question=None)
    p = parties(conn, negotiation_id)
    db.add_message(conn, negotiation_id, p["round"], "system", f"價格談成：{price} 元，等待雙方確認。", price)
    notify_both(
        p,
        f"DealMate：「{p['title']}」談成 {price} 元",
        f"「{p['title']}」價格談成 {price} 元。請回到 Claude 確認成交。",
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
