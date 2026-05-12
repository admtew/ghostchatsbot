"""
Ghost Recovery Bot
ââââââââââââââââââ
Recovers messages your contacts delete from your Telegram Business chats,
and rescues self-destruct media by replying to it or reacting.

Supports both polling (local dev) and webhook (server deploy) modes.

License: MIT
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import html
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    Message,
    MessageReactionUpdated,
)
from dotenv import load_dotenv

# âââââââââââââââââââââââââ Config âââââââââââââââââââââââââ

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Ð½Ðµ Ð·Ð°Ð´Ð°Ð½ Ð² .env")

DB_PATH = os.getenv("DB_PATH", "data.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # ÐµÑÐ»Ð¸ Ð·Ð°Ð´Ð°Ð½ â webhook-ÑÐµÐ¶Ð¸Ð¼
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# ÐÐ¸Ð¼Ð¸Ñ: Ð¼Ð°ÐºÑÐ¸Ð¼ÑÐ¼ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹ Ð² Ð¾Ð´Ð½Ð¾Ð¼ ÑÐ²ÐµÐ´Ð¾Ð¼Ð»ÐµÐ½Ð¸Ð¸ Ð¾Ð± ÑÐ´Ð°Ð»ÐµÐ½Ð¸Ð¸
BATCH_DISPLAY_LIMIT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ghostbot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router(name="ghost")
dp.include_router(router)


# âââââââââââââââââââââââââ Storage âââââââââââââââââââââââââ

_db: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL;")
        _db.execute("PRAGMA synchronous=NORMAL;")
        _db.execute("PRAGMA busy_timeout=5000;")
    return _db


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
            created_at  TEXT NOT NULL,
            PRIMARY KEY (conn_id, chat_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_lookup
            ON messages(conn_id, chat_id, message_id);
        CREATE INDEX IF NOT EXISTS idx_connections_owner
            ON connections(owner_id, enabled);
        """
    )


init_db()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# âââââââââââââââââââââââââ Helpers âââââââââââââââââââââââââ

def save_connection(bc: BusinessConnection) -> None:
    get_db().execute(
        """INSERT INTO connections(id, owner_id, enabled, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               owner_id = excluded.owner_id,
               enabled  = excluded.enabled""",
        (bc.id, bc.user.id, 1 if bc.is_enabled else 0, now_iso()),
    )


def owner_of(conn_id: str) -> int | None:
    row = get_db().execute(
        "SELECT owner_id FROM connections WHERE id=? AND enabled=1",
        (conn_id,),
    ).fetchone()
    return row[0] if row else None


def _classify(msg: Message) -> tuple[str, str | None, dict]:
    """Return (kind, file_id, extra-meta) for a Message."""
    if msg.photo:
        return "photo", msg.photo[-1].file_id, {}
    if msg.video:
        return "video", msg.video.file_id, {}
    if msg.video_note:
        return "video_note", msg.video_note.file_id, {}
    if msg.voice:
        return "voice", msg.voice.file_id, {}
    if msg.audio:
        return "audio", msg.audio.file_id, {}
    if msg.animation:
        return "animation", msg.animation.file_id, {}
    if msg.document:
        return "document", msg.document.file_id, {"name": msg.document.file_name}
    if msg.sticker:
        return "sticker", msg.sticker.file_id, {"emoji": msg.sticker.emoji}
    if msg.contact:
        return "contact", None, {
            "phone": msg.contact.phone_number,
            "name": f"{msg.contact.first_name or ''} {msg.contact.last_name or ''}".strip(),
        }
    if msg.location:
        return "location", None, {
            "lat": msg.location.latitude,
            "lon": msg.location.longitude,
        }
    if msg.text:
        return "text", None, {}
    return msg.content_type or "unknown", None, {}


def _display_name(msg: Message) -> str:
    u = msg.from_user
    if not u:
        return "ÐÐµÐ¸Ð·Ð²ÐµÑÑÐ½ÑÐ¹"
    parts = [p for p in (u.first_name, u.last_name) if p]
    name = " ".join(parts) or "ÐÐµÐ· Ð¸Ð¼ÐµÐ½Ð¸"
    if u.username:
        name = f"{name} (@{u.username})"
    return name


def _safe(text: str | None) -> str:
    """Escape HTML in user-provided text."""
    return html.escape(text) if text else ""


def remember(msg: Message) -> None:
    if not msg.business_connection_id:
        return
    kind, file_id, extra = _classify(msg)
    get_db().execute(
        """INSERT OR REPLACE INTO messages
           (conn_id, chat_id, message_id, sender_id, sender_name,
            kind, text, caption, file_id, extra, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            msg.business_connection_id,
            msg.chat.id,
            msg.message_id,
            msg.from_user.id if msg.from_user else None,
            _display_name(msg),
            kind,
            msg.text,
            msg.caption,
            file_id,
            json.dumps(extra, ensure_ascii=False) if extra else None,
            now_iso(),
        ),
    )


def recall(conn_id: str, chat_id: int, message_id: int) -> dict | None:
    row = get_db().execute(
        """SELECT sender_name, kind, text, caption, file_id, extra
             FROM messages
            WHERE conn_id=? AND chat_id=? AND message_id=?""",
        (conn_id, chat_id, message_id),
    ).fetchone()
    if not row:
        return None
    return {
        "sender_name": row[0],
        "kind": row[1],
        "text": row[2],
        "caption": row[3],
        "file_id": row[4],
        "extra": json.loads(row[5]) if row[5] else {},
    }


def recall_batch(conn_id: str, chat_id: int, message_ids: list[int]) -> list[tuple[int, dict]]:
    """Recall multiple messages at once. Returns list of (message_id, data)."""
    if not message_ids:
        return []
    db = get_db()
    placeholders = ",".join("?" for _ in message_ids)
    rows = db.execute(
        f"""SELECT message_id, sender_name, kind, text, caption, file_id, extra
              FROM messages
             WHERE conn_id=? AND chat_id=? AND message_id IN ({placeholders})
             ORDER BY message_id ASC""",
        [conn_id, chat_id, *message_ids],
    ).fetchall()
    result = []
    for r in rows:
        result.append((r[0], {
            "sender_name": r[1],
            "kind": r[2],
            "text": r[3],
            "caption": r[4],
            "file_id": r[5],
            "extra": json.loads(r[6]) if r[6] else {},
        }))
    return result


# âââââââââââââââââââââââââ Resending âââââââââââââââââââââââââ

async def _send_one(owner: int, item: dict, header: str) -> None:
    """Send a single cached message to the owner."""
    name = _safe(item.get("sender_name") or "ÐÐµÐ¸Ð·Ð²ÐµÑÑÐ½ÑÐ¹")
    info = f"{header}\n<b>ÐÑ:</b> {name}"
    cap = _safe(item.get("caption"))
    body = f"{info}\n\n{cap}" if cap else info
    fid = item.get("file_id")
    kind = item.get("kind")

    try:
        if kind == "text":
            text = _safe(item.get("text") or "")
            await bot.send_message(owner, f"{info}\n\n{text}")
        elif kind == "photo":
            await bot.send_photo(owner, fid, caption=body)
        elif kind == "video":
            await bot.send_video(owner, fid, caption=body)
        elif kind == "animation":
            await bot.send_animation(owner, fid, caption=body)
        elif kind == "voice":
            await bot.send_voice(owner, fid, caption=body)
        elif kind == "audio":
            await bot.send_audio(owner, fid, caption=body)
        elif kind == "document":
            await bot.send_document(owner, fid, caption=body)
        elif kind == "sticker":
            await bot.send_message(owner, info)
            await bot.send_sticker(owner, fid)
        elif kind == "video_note":
            await bot.send_message(owner, info)
            await bot.send_video_note(owner, fid)
        elif kind == "contact":
            extra = item.get("extra", {})
            await bot.send_message(
                owner,
                f"{info}\n\n<b>ÐÐ¾Ð½ÑÐ°ÐºÑ:</b> {_safe(extra.get('name', ''))}\n"
                f"<b>Ð¢ÐµÐ»ÐµÑÐ¾Ð½:</b> {_safe(extra.get('phone', ''))}",
            )
        elif kind == "location":
            extra = item.get("extra", {})
            await bot.send_message(owner, info)
            await bot.send_location(owner, extra["lat"], extra["lon"])
        else:
            await bot.send_message(owner, f"{info}\n\n<i>[{_safe(kind)}]</i>")
    except Exception as e:
        log.warning("resend failed for %s: %s", kind, e)
        await bot.send_message(
            owner,
            f"{info}\n\n<i>ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¿ÐµÑÐµÑÐ»Ð°ÑÑ ({e.__class__.__name__}).</i>",
        )


async def forward_deleted_batch(
    owner: int,
    items: list[tuple[int, dict]],
    chat_title: str | None,
) -> None:
    """Send a batch of deleted messages to the owner with a single header."""
    count = len(items)
    chat_info = f" Ð² ÑÐ°ÑÐµ <b>{_safe(chat_title)}</b>" if chat_title else ""

    if count == 1:
        # ÐÐ´Ð½Ð¾ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ â ÐºÐ°Ðº ÑÐ°Ð½ÑÑÐµ, ÐºÐ¾Ð¼Ð¿Ð°ÐºÑÐ½Ð¾
        _, item = items[0]
        await _send_one(owner, item, "ð <b>Ð£Ð´Ð°Ð»ÑÐ½Ð½Ð¾Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ</b>")
        return

    # ÐÐµÑÐºÐ¾Ð»ÑÐºÐ¾ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹ â ÑÐ½Ð°ÑÐ°Ð»Ð° Ð·Ð°Ð³Ð¾Ð»Ð¾Ð²Ð¾Ðº, Ð¿Ð¾ÑÐ¾Ð¼ ÐºÐ°Ð¶Ð´Ð¾Ðµ
    header_text = (
        f"ð <b>Ð£Ð´Ð°Ð»ÐµÐ½Ð¾ {count} ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹</b>{chat_info}\n"
        f"{'â' * 20}"
    )
    await bot.send_message(owner, header_text)

    for i, (mid, item) in enumerate(items[:BATCH_DISPLAY_LIMIT], 1):
        await _send_one(owner, item, f"<b>[{i}/{count}]</b>")
        # ÐÐµÐ±Ð¾Ð»ÑÑÐ°Ñ Ð·Ð°Ð´ÐµÑÐ¶ÐºÐ° ÑÑÐ¾Ð±Ñ Ð½Ðµ ÑÐ»Ð¾Ð²Ð¸ÑÑ rate limit Ð¾Ñ Telegram
        if i % 5 == 0:
            await asyncio.sleep(0.5)

    if count > BATCH_DISPLAY_LIMIT:
        await bot.send_message(
            owner,
            f"<i>...Ð¸ ÐµÑÑ {count - BATCH_DISPLAY_LIMIT} ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹ "
            f"(Ð¿Ð¾ÐºÐ°Ð·Ð°Ð½Ñ Ð¿ÐµÑÐ²ÑÐµ {BATCH_DISPLAY_LIMIT}).</i>",
        )


async def forward_cached(owner: int, item: dict, header: str) -> None:
    """Send a single cached message (for reaction/reply saves)."""
    await _send_one(owner, item, header)


# âââââââââââââââââââââââââ Business handlers âââââââââââââââââââââââââ

@router.business_connection()
async def on_connect(bc: BusinessConnection) -> None:
    save_connection(bc)
    try:
        if bc.is_enabled:
            await bot.send_message(
                bc.user.id,
                "â <b>ÐÐ¾Ñ Ð¿Ð¾Ð´ÐºÐ»ÑÑÑÐ½</b>\n\n"
                "Ð¯ ÑÐ¸ÑÐ¾ ÑÐ¾ÑÑÐ°Ð½ÑÑ Ð²ÑÐ¾Ð´ÑÑÐ¸Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ Ð¸ Ð¿ÑÐ¸ÑÐ»Ñ Ð²ÑÑ, "
                "ÑÑÐ¾ ÑÐ¾Ð±ÐµÑÐµÐ´Ð½Ð¸Ðº ÑÐ´Ð°Ð»Ð¸Ñ â Ð´Ð°Ð¶Ðµ ÐµÑÐ»Ð¸ ÑÐ´Ð°Ð»Ð¸Ñ ÑÑÐ°Ð·Ñ Ð¿Ð°ÑÐºÑ.\n\n"
                "ð¸ <b>Ð¡Ð¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ Ñ ÑÐ°Ð¹Ð¼ÐµÑÐ¾Ð¼</b> â Ð¾ÑÐ²ÐµÑÑ Ð½Ð° ÑÐ°ÐºÐ¾Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ "
                "Ð»ÑÐ±ÑÐ¼ ÑÐ¸Ð¼Ð²Ð¾Ð»Ð¾Ð¼ Ð¸Ð»Ð¸ Ð¿Ð¾ÑÑÐ°Ð²Ñ ÑÐµÐ°ÐºÑÐ¸Ñ, Ñ Ð¿ÑÐ¸ÑÐ»Ñ ÐºÐ¾Ð¿Ð¸Ñ.\n\n"
                "ð ÐÐ°Ð½Ð½ÑÐµ ÑÑÐ°Ð½ÑÑÑÑ ÑÐ¾Ð»ÑÐºÐ¾ Ð½Ð° ÑÐ²Ð¾ÑÐ¼ ÑÐµÑÐ²ÐµÑÐµ.\n\n"
                "/help â ÑÐ¿ÑÐ°Ð²ÐºÐ° â¢ /status â ÑÑÐ°ÑÑÑ â¢ /wipe â Ð¾ÑÐ¸ÑÑÐ¸ÑÑ ÐºÑÑ",
            )
        else:
            await bot.send_message(
                bc.user.id, "ð ÐÐ¾Ñ Ð¾ÑÐºÐ»ÑÑÑÐ½, Ð¾ÑÑÐ»ÐµÐ¶Ð¸Ð²Ð°Ð½Ð¸Ðµ Ð¾ÑÑÐ°Ð½Ð¾Ð²Ð»ÐµÐ½Ð¾."
            )
    except Exception as e:
        log.warning("notify on connect failed: %s", e)


@router.business_message()
async def on_business_message(msg: Message) -> None:
    remember(msg)

    # Self-destruct rescue: owner replies â resend the cached original
    owner = owner_of(msg.business_connection_id or "")
    if (
        owner
        and msg.from_user
        and msg.from_user.id == owner
        and msg.reply_to_message
    ):
        r = msg.reply_to_message
        cached = recall(msg.business_connection_id, r.chat.id, r.message_id)
        if cached:
            await forward_cached(owner, cached, "ð¸ <b>Ð¡Ð¾ÑÑÐ°Ð½ÐµÐ½Ð¾</b>")


@router.edited_business_message()
async def on_edited(msg: Message) -> None:
    if not msg.business_connection_id:
        return
    existing = recall(msg.business_connection_id, msg.chat.id, msg.message_id)
    if existing is None:
        remember(msg)


@router.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted) -> None:
    owner = owner_of(event.business_connection_id)
    if not owner:
        return

    # ÐÐ°ÑÑÐµÐ²ÑÐ¹ Ð·Ð°Ð¿ÑÐ¾Ñ â Ð¾Ð´Ð¸Ð½ SQL Ð²Ð¼ÐµÑÑÐ¾ N
    items = recall_batch(
        event.business_connection_id,
        event.chat.id,
        list(event.message_ids),
    )

    if not items:
        # Ð¡Ð¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ Ð½Ðµ Ð±ÑÐ»Ð¸ Ð² ÐºÑÑÐµ â ÑÐ²ÐµÐ´Ð¾Ð¼Ð»ÑÐµÐ¼ ÑÑÐ¾ ÑÑÐ¾-ÑÐ¾ ÑÐ´Ð°Ð»Ð¸Ð»Ð¸ Ð½Ð¾ Ð¼Ñ Ð½Ðµ ÑÑÐ¿ÐµÐ»Ð¸ ÑÐ¾ÑÑÐ°Ð½Ð¸ÑÑ
        count = len(event.message_ids)
        chat_name = ""
        if event.chat:
            fn = event.chat.first_name or ""
            ln = event.chat.last_name or ""
            chat_name = f" ({_safe(fn)} {_safe(ln)}".strip() + ")"
        await bot.send_message(
            owner,
            f"ð Ð£Ð´Ð°Ð»ÐµÐ½Ð¾ <b>{count}</b> ÑÐ¾Ð¾Ð±Ñ.{chat_name}, "
            f"Ð½Ð¾ Ð¾Ð½Ð¸ Ð½Ðµ Ð±ÑÐ»Ð¸ Ð² ÐºÑÑÐµ (Ð±Ð¾Ñ Ð¼Ð¾Ð³ Ð±ÑÑÑ Ð²ÑÐºÐ»ÑÑÐµÐ½).",
        )
        return

    # ÐÐ¿ÑÐµÐ´ÐµÐ»ÑÐµÐ¼ Ð½Ð°Ð·Ð²Ð°Ð½Ð¸Ðµ ÑÐ°ÑÐ° Ð´Ð»Ñ Ð·Ð°Ð³Ð¾Ð»Ð¾Ð²ÐºÐ°
    chat_title = None
    if event.chat:
        parts = [event.chat.first_name, event.chat.last_name]
        chat_title = " ".join(p for p in parts if p) or None

    await forward_deleted_batch(owner, items, chat_title)

    # Ð£Ð²ÐµÐ´Ð¾Ð¼Ð»ÑÐµÐ¼ ÐµÑÐ»Ð¸ ÑÐ°ÑÑÑ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹ Ð½Ðµ Ð±ÑÐ»Ð° Ð² ÐºÑÑÐµ
    found_ids = {m_id for m_id, _ in items}
    missing = [m_id for m_id in event.message_ids if m_id not in found_ids]
    if missing:
        await bot.send_message(
            owner,
            f"<i>ÐÑÑ {len(missing)} ÑÐ´Ð°Ð». ÑÐ¾Ð¾Ð±Ñ. Ð½Ðµ Ð±ÑÐ»Ð¸ Ð² ÐºÑÑÐµ.</i>",
        )


@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated) -> None:
    """Rescue self-destruct media when the owner adds a reaction."""
    if not event.new_reaction or len(event.new_reaction) <= len(event.old_reaction or []):
        return
    if not event.user:
        return

    bc_id = getattr(event, "business_connection_id", None)

    if bc_id:
        owner = owner_of(bc_id)
        if not owner or event.user.id != owner:
            return
        cached = recall(bc_id, event.chat.id, event.message_id)
    else:
        db = get_db()
        row = db.execute(
            """SELECT m.conn_id, m.sender_name, m.kind, m.text, m.caption,
                      m.file_id, m.extra
                 FROM messages m
                 JOIN connections c ON c.id = m.conn_id
                WHERE c.owner_id = ?
                  AND c.enabled  = 1
                  AND m.chat_id  = ?
                  AND m.message_id = ?
                LIMIT 1""",
            (event.user.id, event.chat.id, event.message_id),
        ).fetchone()
        if not row:
            return
        owner = event.user.id
        cached = {
            "sender_name": row[1],
            "kind":        row[2],
            "text":        row[3],
            "caption":     row[4],
            "file_id":     row[5],
            "extra":       json.loads(row[6]) if row[6] else {},
        }

    if cached:
        await forward_cached(owner, cached, "â¤ï¸ <b>Ð¡Ð¾ÑÑÐ°Ð½ÐµÐ½Ð¾ Ð¿Ð¾ ÑÐµÐ°ÐºÑÐ¸Ð¸</b>")


# âââââââââââââââââââââââââ Private chat commands âââââââââââââââââââââââââ

@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "ð» <b>Ghost Recovery Bot</b>\n\n"
        "Ð¯ Ð¿ÐµÑÐµÑÐ²Ð°ÑÑÐ²Ð°Ñ Ð¸ ÑÐ¾ÑÑÐ°Ð½ÑÑ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ Ð¸Ð· ÑÐ²Ð¾Ð¸Ñ Ð±Ð¸Ð·Ð½ÐµÑ-ÑÐ°ÑÐ¾Ð². "
        "ÐÑÐ»Ð¸ ÑÐ¾Ð±ÐµÑÐµÐ´Ð½Ð¸Ðº ÑÐ´Ð°Ð»Ð¸Ñ ÑÑÐ¾-ÑÐ¾ â ÑÑ Ð¿Ð¾Ð»ÑÑÐ¸ÑÑ ÐºÐ¾Ð¿Ð¸Ñ.\n\n"
        "ÐÑÐ¿ÑÐ°Ð²Ñ /help ÑÑÐ¾Ð±Ñ ÑÐ·Ð½Ð°ÑÑ ÐºÐ°Ðº Ð¿Ð¾Ð´ÐºÐ»ÑÑÐ¸ÑÑ."
    )


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "ð <b>ÐÐ°Ðº ÑÐ°Ð±Ð¾ÑÐ°ÐµÑ Ghost Recovery Bot</b>\n\n"
        "<b>ÐÐ¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¸Ðµ:</b>\n"
        "1. ÐÑÐºÑÐ¾Ð¹ Telegram â <b>ÐÐ°ÑÑÑÐ¾Ð¹ÐºÐ¸</b> â <b>Telegram Business</b>\n"
        "2. Ð Ð°Ð·Ð´ÐµÐ» <b>Ð§Ð°Ñ-Ð±Ð¾ÑÑ</b> â Ð²Ð²ÐµÐ´Ð¸ ÑÐ·ÐµÑÐ½ÐµÐ¹Ð¼ ÑÑÐ¾Ð³Ð¾ Ð±Ð¾ÑÐ°\n"
        "3. ÐÐ¾Ð´ÐºÐ»ÑÑÐ¸ Ð¸ Ð´Ð°Ð¹ ÑÐ°Ð·ÑÐµÑÐµÐ½Ð¸Ñ Ð½Ð° ÑÑÐµÐ½Ð¸Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹\n\n"
        "<b>Ð§ÑÐ¾ Ð´ÐµÐ»Ð°ÐµÑ Ð±Ð¾Ñ:</b>\n"
        "â¢ Ð¢Ð¸ÑÐ¾ ÑÐ¾ÑÑÐ°Ð½ÑÐµÑ Ð²ÑÐµ Ð²ÑÐ¾Ð´ÑÑÐ¸Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ Ð² Ð±Ð¸Ð·Ð½ÐµÑ-ÑÐ°ÑÐ°Ñ\n"
        "â¢ ÐÑÐ»Ð¸ ÑÐ¾Ð±ÐµÑÐµÐ´Ð½Ð¸Ðº ÑÐ´Ð°Ð»Ð¸Ñ 1 Ð¸Ð»Ð¸ 50 ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹ â ÑÑ Ð¿Ð¾Ð»ÑÑÐ¸ÑÑ Ð¸Ñ Ð²ÑÐµ\n"
        "â¢ Ð¢ÐµÐºÑÑ, ÑÐ¾ÑÐ¾, Ð²Ð¸Ð´ÐµÐ¾, Ð³Ð¾Ð»Ð¾ÑÐ¾Ð²ÑÐµ, Ð´Ð¾ÐºÑÐ¼ÐµÐ½ÑÑ, ÑÑÐ¸ÐºÐµÑÑ â Ð²ÑÑ ÑÐ¾ÑÑÐ°Ð½ÑÐµÑÑÑ\n\n"
        "ð¸ <b>Ð¡Ð¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ Ñ ÑÐ°Ð¹Ð¼ÐµÑÐ¾Ð¼:</b>\n"
        "ÐÑÐ²ÐµÑÑ Ð½Ð° ÑÐ°ÐºÐ¾Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ Ð»ÑÐ±ÑÐ¼ ÑÐ¸Ð¼Ð²Ð¾Ð»Ð¾Ð¼ Ð¸Ð»Ð¸ Ð¿Ð¾ÑÑÐ°Ð²Ñ ÑÐµÐ°ÐºÑÐ¸Ñ â "
        "Ð±Ð¾Ñ Ð¿ÑÐ¸ÑÐ»ÑÑ ÑÐµÐ±Ðµ ÐºÐ¾Ð¿Ð¸Ñ Ð´Ð¾ ÑÐ¾Ð³Ð¾ ÐºÐ°Ðº Ð¾Ð½Ð¾ Ð¸ÑÑÐµÐ·Ð½ÐµÑ.\n\n"
        "<b>ÐÐ¾Ð¼Ð°Ð½Ð´Ñ:</b>\n"
        "/start â Ð¿ÑÐ¸Ð²ÐµÑÑÑÐ²Ð¸Ðµ\n"
        "/help â ÑÑÐ° ÑÐ¿ÑÐ°Ð²ÐºÐ°\n"
        "/status â ÑÑÐ°ÑÑÑ Ð¿Ð¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¸Ñ Ð¸ ÐºÑÑÐ°\n"
        "/wipe â Ð¿Ð¾Ð»Ð½Ð¾ÑÑÑÑ Ð¾ÑÐ¸ÑÑÐ¸ÑÑ ÑÐ¾ÑÑÐ°Ð½ÑÐ½Ð½ÑÐµ Ð´Ð°Ð½Ð½ÑÐµ\n\n"
        "ð ÐÑÐµ Ð´Ð°Ð½Ð½ÑÐµ ÑÑÐ°Ð½ÑÑÑÑ ÑÐ¾Ð»ÑÐºÐ¾ Ð½Ð° ÑÐ²Ð¾ÑÐ¼ ÑÐµÑÐ²ÐµÑÐµ. "
        "ÐÐ¾Ñ Ð½Ðµ Ð¿ÐµÑÐµÐ´Ð°ÑÑ Ð¸Ñ ÑÑÐµÑÑÐ¸Ð¼ Ð»Ð¸ÑÐ°Ð¼."
    )


@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    uid = msg.from_user.id
    db = get_db()
    connected = db.execute(
        "SELECT COUNT(*) FROM connections WHERE owner_id=? AND enabled=1",
        (uid,),
    ).fetchone()[0]
    cached = db.execute(
        """SELECT COUNT(*) FROM messages
            WHERE conn_id IN (SELECT id FROM connections WHERE owner_id=?)""",
        (uid,),
    ).fetchone()[0]
    line = "â ÐÐ¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¾" if connected else "â ÐÐµ Ð¿Ð¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¾"
    await msg.answer(
        f"<b>Ð¡ÑÐ°ÑÑÑ:</b> {line}\n"
        f"<b>ÐÐºÑÐ¸Ð²Ð½ÑÑ Ð¿Ð¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¸Ð¹:</b> {connected}\n"
        f"<b>Ð¡Ð¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹ Ð² ÐºÑÑÐµ:</b> {cached}"
    )


@router.message(Command("wipe"))
async def cmd_wipe(msg: Message) -> None:
    uid = msg.from_user.id
    db = get_db()
    result = db.execute(
        """DELETE FROM messages
            WHERE conn_id IN (SELECT id FROM connections WHERE owner_id=?)""",
        (uid,),
    )
    await msg.answer(f"ð§¹ ÐÑÑ Ð¾ÑÐ¸ÑÐµÐ½. Ð£Ð´Ð°Ð»ÐµÐ½Ð¾ Ð·Ð°Ð¿Ð¸ÑÐµÐ¹: {result.rowcount}")


# âââââââââââââââââââââââââ Entry âââââââââââââââââââââââââ

ALLOWED_UPDATES = [
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "message_reaction",
]


async def main() -> None:
    log.info("Ghost Recovery Bot starting...")

    from aiohttp import web

    app = web.Application()

    async def handle_health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)

    if WEBHOOK_URL:
        # Webhook mode (for server deployment)
        from aiogram.types import Update

        full_url = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        await bot.set_webhook(
            full_url,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=False,
        )
        log.info("Webhook set: %s", full_url)

        async def handle_webhook(request: web.Request) -> web.Response:
            data = await request.json()
            update = Update.model_validate(data, context={"bot": bot})
            await dp.feed_update(bot=bot, update=update)
            return web.Response(text="ok")

        app.router.add_post(WEBHOOK_PATH, handle_webhook)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PORT)
        log.info("Listening on %s:%s", HOST, PORT)
        await site.start()
        await asyncio.Event().wait()
    else:
        # Polling mode (local dev) — health server still runs for compatibility
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PORT)
        await site.start()
        log.info("Health server on %s:%s, starting polling...", HOST, PORT)

        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped.")
