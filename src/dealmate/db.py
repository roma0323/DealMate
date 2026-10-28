"""SQLite storage: one file, five tables (see the technical design doc)."""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DEALMATE_DB", "dealmate.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS listings (
    id                 INTEGER PRIMARY KEY,
    seller_id          INTEGER NOT NULL REFERENCES users(id),
    category           TEXT NOT NULL,
    title              TEXT NOT NULL,
    condition          TEXT,
    location           TEXT,
    ask_price          INTEGER NOT NULL,
    floor_price        INTEGER NOT NULL,
    auto_accept_price  INTEGER NOT NULL,
    photo_path         TEXT,
    status             TEXT NOT NULL DEFAULT 'open',   -- open / matched / sold / closed
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wants (
    id           INTEGER PRIMARY KEY,
    buyer_id     INTEGER NOT NULL REFERENCES users(id),
    category     TEXT NOT NULL,
    description  TEXT NOT NULL,
    location     TEXT,
    budget       INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',          -- open / matched / bought / closed
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS negotiations (
    id                INTEGER PRIMARY KEY,
    listing_id        INTEGER NOT NULL REFERENCES listings(id),
    want_id           INTEGER NOT NULL REFERENCES wants(id),
    -- matched / negotiating / waiting_user / agreed / closed / failed
    state             TEXT NOT NULL DEFAULT 'matched',
    round             INTEGER NOT NULL DEFAULT 0,
    seller_offer      INTEGER,
    buyer_offer       INTEGER,
    agreed_price      INTEGER,
    pending_party     TEXT,                              -- 'seller' / 'buyer' while waiting_user
    pending_question  TEXT,
    seller_confirmed  INTEGER NOT NULL DEFAULT 0,
    buyer_confirmed   INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Public offers only. Floor prices and budgets never go in here.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    negotiation_id  INTEGER NOT NULL REFERENCES negotiations(id),
    round           INTEGER NOT NULL,
    sender          TEXT NOT NULL,                       -- seller_agent / buyer_agent / system
    price           INTEGER,
    text            TEXT NOT NULL,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Cheap with IF NOT EXISTS, and recreates the tables if dealmate.db is deleted while running.
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def row(conn, sql: str, *args) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def rows(conn, sql: str, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_user(conn, name: str) -> dict | None:
    return row(conn, "SELECT * FROM users WHERE name = ?", name)


def add_message(conn, negotiation_id: int, round_: int, sender: str, text: str, price: int | None = None) -> None:
    conn.execute(
        "INSERT INTO messages (negotiation_id, round, sender, price, text) VALUES (?, ?, ?, ?, ?)",
        (negotiation_id, round_, sender, price, text),
    )


def set_negotiation(conn, negotiation_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE negotiations SET {cols}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*fields.values(), negotiation_id),
    )
