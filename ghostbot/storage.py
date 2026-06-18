"""Persistence layer: SQLite + a hot in-memory write-through cache.

Why the in-memory layer matters
-------------------------------
In webhook mode every Telegram update is a separate HTTP request handled
concurrently. A contact can send a message and delete it a split second later
(classic for self-destruct media). The ``deleted_business_messages`` update may
then be processed *before* the ``business_message`` update has finished its DB
write — so a plain ``SELECT`` finds nothing and the copy is lost.

``remember()`` writes synchronously into ``_mem`` *and* SQLite. ``recall()``
reads ``_mem`` first. Because the dict write happens in the same synchronous
block as the message handler, any delete handler that runs *after* the message
handler sees the data instantly, with no dependency on DB commit timing. The
remaining race (delete handled before the message update even arrives) is
covered by the retry loop in the handlers layer.
"""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from datetime import datetime, timezone

from aiogram.types import Message

from .config import DB_PATH, MEMORY_CACHE_SIZE, log
from .formatting import classify, display_name

# ============================ Connection ============================

_db: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL;")
        _db.execute("PRAGMA synchronous=NORMAL;")
        _db.execute("PRAGMA busy_timeout=5000;")
    return _db


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS connections (
            id         TEXT PRIMARY KEY,
            owner_id   INTEGER NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            conn_id     TEXT    NOT NULL,
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            sender_id   INTEGER,
            sender_name TEXT,
            kind        TEXT,
            text        TEXT,
            caption     TEXT,
            file_id     TEXT,
            extra       TEXT,
            edited      INTEGER NOT NULL DEFAULT 0,
            protected   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (conn_id, chat_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_lookup
            ON messages(conn_id, chat_id, message_id);
        CREATE INDEX IF NOT EXISTS idx_connections_owner
            ON connections(owner_id, enabled);
        CREATE TABLE IF NOT EXISTS mutes (
            conn_id  TEXT    NOT NULL,
            chat_id  INTEGER NOT NULL,
            until    INTEGER,
            PRIMARY KEY (conn_id, chat_id)
        );
        """
    )
    # Lightweight migration for databases created by older versions.
    if "edited" not in _columns(db, "messages"):
        db.execute("ALTER TABLE messages ADD COLUMN edited INTEGER NOT NULL DEFAULT 0")
        log.info("migrated messages table: added 'edited' column")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================ Hot cache ============================

_Key = tuple[str, int, int]  # (conn_id, chat_id, message_id)
_mem: "OrderedDict[_Key, dict]" = OrderedDict()


def _mem_put(key: _Key, data: dict) -> None:
    _mem[key] = data
    _mem.move_to_end(key)
    while len(_mem) > MEMORY_CACHE_SIZE:
        _mem.popitem(last=False)


def _mem_get(key: _Key) -> dict | None:
    return _mem.get(key)


# ============================ Connections ============================

def save_connection(conn_id: str, owner_id: int, enabled: bool = True) -> None:
    get_db().execute(
        """INSERT INTO connections(id, owner_id, enabled, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               owner_id = excluded.owner_id,
               enabled  = excluded.enabled""",
        (conn_id, owner_id, 1 if enabled else 0, now_iso()),
    )


def owner_of(conn_id: str) -> int | None:
    row = get_db().execute(
        "SELECT owner_id FROM connections WHERE id=? AND enabled=1",
        (conn_id,),
    ).fetchone()
    return row[0] if row else None


# ============================ Messages ============================

def _row_to_dict(row: tuple) -> dict:
    return {
        "sender_id": row[0],
        "sender_name": row[1],
        "kind": row[2],
        "text": row[3],
        "caption": row[4],
        "file_id": row[5],
        "extra": json.loads(row[6]) if row[6] else {},
        "edited": bool(row[7]),
        "protected": bool(row[8]),
    }


def remember(msg: Message, *, edited: bool = False) -> None:
    """Store (or overwrite) a business message in cache and DB."""
    if not msg.business_connection_id:
        return
    kind, file_id, extra = classify(msg)
    key: _Key = (msg.business_connection_id, msg.chat.id, msg.message_id)
    data = {
        "sender_id": msg.from_user.id if msg.from_user else None,
        "sender_name": display_name(msg),
        "kind": kind,
        "text": msg.text,
        "caption": msg.caption,
        "file_id": file_id,
        "extra": extra or {},
        "edited": edited,
        "protected": False,
    }
    # 1) Hot cache first — this is what makes recovery race-proof.
    _mem_put(key, data)
    # 2) Durable copy.
    try:
        get_db().execute(
            """INSERT OR REPLACE INTO messages
               (conn_id, chat_id, message_id, sender_id, sender_name,
                kind, text, caption, file_id, extra, edited, protected, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (
                key[0], key[1], key[2],
                data["sender_id"], data["sender_name"], kind,
                data["text"], data["caption"], file_id,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                1 if edited else 0,
                now_iso(),
            ),
        )
    except Exception as e:  # pragma: no cover - defensive
        log.error("DB write failed for msg=%s: %s (kept in memory)", msg.message_id, e)


def recall(conn_id: str, chat_id: int, message_id: int) -> dict | None:
    key: _Key = (conn_id, chat_id, message_id)
    hit = _mem_get(key)
    if hit is not None:
        return hit
    row = get_db().execute(
        """SELECT sender_id, sender_name, kind, text, caption, file_id, extra, edited, protected
             FROM messages
            WHERE conn_id=? AND chat_id=? AND message_id=?""",
        key,
    ).fetchone()
    return _row_to_dict(row) if row else None


def recall_batch(conn_id: str, chat_id: int, message_ids: list[int]) -> list[tuple[int, dict]]:
    """Recall multiple messages, preserving the requested order.

    Cache hits are served from memory; the rest are fetched from SQLite in one
    query. Missing ids are simply absent from the result.
    """
    if not message_ids:
        return []

    found: dict[int, dict] = {}
    misses: list[int] = []
    for mid in message_ids:
        hit = _mem_get((conn_id, chat_id, mid))
        if hit is not None:
            found[mid] = hit
        else:
            misses.append(mid)

    if misses:
        placeholders = ",".join("?" for _ in misses)
        rows = get_db().execute(
            f"""SELECT message_id, sender_id, sender_name, kind, text, caption,
                       file_id, extra, edited, protected
                  FROM messages
                 WHERE conn_id=? AND chat_id=? AND message_id IN ({placeholders})""",
            [conn_id, chat_id, *misses],
        ).fetchall()
        for r in rows:
            found[r[0]] = _row_to_dict(r[1:])

    return [(mid, found[mid]) for mid in message_ids if mid in found]


def wipe_owner(owner_id: int) -> int:
    """Delete everything stored for an owner. Returns rows removed."""
    db = get_db()
    conn_ids = [r[0] for r in db.execute(
        "SELECT id FROM connections WHERE owner_id=?", (owner_id,)
    ).fetchall()]
    removed = 0
    for cid in conn_ids:
        cur = db.execute("DELETE FROM messages WHERE conn_id=?", (cid,))
        removed += cur.rowcount or 0
        db.execute("DELETE FROM mutes WHERE conn_id=?", (cid,))
    # Drop the in-memory copies too.
    for key in [k for k in _mem if k[0] in conn_ids]:
        _mem.pop(key, None)
    return removed


def count_for_owner(owner_id: int) -> int:
    row = get_db().execute(
        """SELECT COUNT(*) FROM messages
            WHERE conn_id IN (SELECT id FROM connections WHERE owner_id=?)""",
        (owner_id,),
    ).fetchone()
    return row[0] if row else 0


# ============================ Mutes ============================

def set_mute(conn_id: str, chat_id: int, until: int | None) -> None:
    get_db().execute(
        """INSERT INTO mutes(conn_id, chat_id, until) VALUES(?,?,?)
           ON CONFLICT(conn_id, chat_id) DO UPDATE SET until = excluded.until""",
        (conn_id, chat_id, until),
    )


def clear_mute(conn_id: str, chat_id: int) -> None:
    get_db().execute(
        "DELETE FROM mutes WHERE conn_id=? AND chat_id=?", (conn_id, chat_id)
    )


def mute_until(conn_id: str, chat_id: int) -> int | None | bool:
    """Return ``False`` if not muted, else the unix-ts (or ``None`` = forever).

    Expired mutes are cleared automatically.
    """
    import time

    row = get_db().execute(
        "SELECT until FROM mutes WHERE conn_id=? AND chat_id=?", (conn_id, chat_id)
    ).fetchone()
    if not row:
        return False
    until = row[0]
    if until is not None and time.time() >= until:
        clear_mute(conn_id, chat_id)
        return False
    return until
