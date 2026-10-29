"""SQLite storage: one file, six tables.

Structured fields (specs, conditions, meetup slots and stations) are stored as JSON text.
"""

import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DEALMATE_DB", "dealmate.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL,
    slots       TEXT NOT NULL DEFAULT '[]',   -- when they can meet up: [{"day"|"date", "start", "end"}]
    stations    TEXT NOT NULL DEFAULT '[]',   -- where they can meet up: ["南港", "汐止"]
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS listings (
    id                 INTEGER PRIMARY KEY,
    seller_id          INTEGER NOT NULL REFERENCES users(id),
    category           TEXT NOT NULL,
    title              TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    condition          TEXT,
    location           TEXT,
    price              INTEGER NOT NULL,              -- fixed price, no bargaining
    specs              TEXT NOT NULL DEFAULT '[]',    -- [{"key", "label", "value", "source"}]
    size               TEXT,                          -- e.g. "寬 30 × 深 30 × 高 150 cm"
    needs_disassembly  INTEGER NOT NULL DEFAULT 0,
    needs_truck        INTEGER NOT NULL DEFAULT 0,
    photo_path         TEXT,
    upload_token       TEXT,                          -- lets the listing card upload a photo
    status             TEXT NOT NULL DEFAULT 'open',  -- open / reserved / sold / closed
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wants (
    id           INTEGER PRIMARY KEY,
    buyer_id     INTEGER NOT NULL REFERENCES users(id),
    category     TEXT NOT NULL,
    description  TEXT NOT NULL,
    budget_min   INTEGER NOT NULL DEFAULT 0,
    budget_max   INTEGER NOT NULL,
    conditions   TEXT NOT NULL DEFAULT '[]',          -- [{"key", "op", "value"}]
    location     TEXT,
    status       TEXT NOT NULL DEFAULT 'open',        -- open / closed; stays open after a match
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Which want was already told about which listing, so a buyer is emailed once per item.
CREATE TABLE IF NOT EXISTS want_matches (
    want_id     INTEGER NOT NULL REFERENCES wants(id),
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (want_id, listing_id)
);

CREATE TABLE IF NOT EXISTS questions (
    id           INTEGER PRIMARY KEY,
    listing_id   INTEGER NOT NULL REFERENCES listings(id),
    buyer_id     INTEGER NOT NULL REFERENCES users(id),
    question     TEXT NOT NULL,
    answer       TEXT,
    public       INTEGER NOT NULL DEFAULT 0,          -- 1 = shown on the listing for every buyer
    status       TEXT NOT NULL DEFAULT 'pending',     -- pending / answered
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    answered_at  TEXT
);

CREATE TABLE IF NOT EXISTS deals (
    id                INTEGER PRIMARY KEY,
    listing_id        INTEGER NOT NULL REFERENCES listings(id),
    buyer_id          INTEGER NOT NULL REFERENCES users(id),
    price             INTEGER NOT NULL,
    -- requested (buyer asked) -> confirmed (seller agreed) -> scheduled (meetup set); or cancelled
    state             TEXT NOT NULL DEFAULT 'requested',
    seller_confirmed  INTEGER NOT NULL DEFAULT 0,
    buyer_confirmed   INTEGER NOT NULL DEFAULT 0,
    meetup_at         TEXT,                           -- "2026-11-12T19:00", Asia/Taipei
    meetup_station    TEXT,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# Columns holding JSON text, decoded by row() / rows(); also aliases ending in them, like seller_slots.
JSON_COLUMNS = {"slots", "stations", "specs", "conditions"}


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
        # dealmate.db from the negotiation version has a different schema: refuse to run on it.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'negotiations'").fetchone():
            raise SystemExit(f"{DB_PATH} 是舊版（議價版）的資料庫，請停止 server 後刪除它再重新啟動。")


def _decode(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in d:
        if k.rsplit("_", 1)[-1] in JSON_COLUMNS and isinstance(d[k], str):
            d[k] = json.loads(d[k]) if d[k] else []
    return d


def row(conn, sql: str, *args) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return _decode(r) if r else None


def rows(conn, sql: str, *args) -> list[dict]:
    return [_decode(r) for r in conn.execute(sql, args).fetchall()]


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def get_user(conn, name: str) -> dict | None:
    return row(conn, "SELECT * FROM users WHERE name = ?", name)


def set_availability(conn, user_id: int, slots: list | None, stations: list | None) -> None:
    """Replace the user's meetup slots and/or stations; None leaves that one unchanged."""
    if slots is not None:
        conn.execute("UPDATE users SET slots = ? WHERE id = ?", (dumps(slots), user_id))
    if stations is not None:
        conn.execute("UPDATE users SET stations = ? WHERE id = ?", (dumps(stations), user_id))


def set_deal(conn, deal_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE deals SET {cols}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*fields.values(), deal_id),
    )
