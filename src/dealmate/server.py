"""DealMate MCP server: the tools Claude calls through the custom connector."""

import logging
import os
from typing import Literal

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import db, engine

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

mcp = MCPServer(
    name="DealMate",
    instructions=(
        "DealMate gives each user an AI agent that buys or sells second-hand items for them. "
        "The user's agent negotiates with the other side's agent on the DealMate server; "
        "the user only describes what they want, answers questions when asked, and confirms the final deal. "
        "Always call register_user first with the user's name and email if they have not registered. "
        "Pass the same `user` name on every later call. Reply to the user in Traditional Chinese."
    ),
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


@mcp.tool()
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
        neg_id = engine.find_match_for_listing(conn, listing_id)
    if neg_id:
        engine.start_negotiation_async(neg_id)
    return {"listing_id": listing_id, "matched": bool(neg_id), "negotiation_id": neg_id}


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


@mcp.tool()
def check_status(user: str) -> dict:
    """Show the user's listings, wants and negotiations, including the offer history
    and any question their agent is waiting for them to answer.

    Call this whenever the user asks how things are going.
    """
    with db.connect() as conn:
        u = _require_user(conn, user)
        listings = db.rows(
            conn,
            "SELECT id, category, title, ask_price, floor_price, auto_accept_price, status "
            "FROM listings WHERE seller_id = ?",
            u["id"],
        )
        wants = db.rows(
            conn, "SELECT id, category, description, budget, status FROM wants WHERE buyer_id = ?", u["id"]
        )
        negs = db.rows(
            conn,
            """
            SELECT n.id, n.state, n.round, n.agreed_price, n.pending_party, n.pending_question,
                   n.seller_confirmed, n.buyer_confirmed, l.title,
                   CASE WHEN l.seller_id = ? THEN 'seller' ELSE 'buyer' END AS my_role
            FROM negotiations n
            JOIN listings l ON l.id = n.listing_id
            JOIN wants w ON w.id = n.want_id
            WHERE l.seller_id = ? OR w.buyer_id = ?
            ORDER BY n.id
            """,
            u["id"], u["id"], u["id"],
        )
        for n in negs:
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
