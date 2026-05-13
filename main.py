"""
Ghost Recovery Bot
------------------
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

# ========================= Config =========================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")

_default_db = "/data/ghost.db" if os.path.isdir("/data") else "data.db"
DB_PATH = os.getenv("DB_PATH", _default_db)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # если задан — webhook-режим
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

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


# ========================= Storage =========================

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
            protected   INTEGER NOT NULL DEFAULT 0,
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


# ========================= Helpers =========================

def save_connection(bc: BusinessConnection) -> None:
    get_db().execute(
        """INSERT INTO connections(id, owner_id, enabled, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               owner_id = excluded.owner_id,
               enabled  = excluded.enabled""",
        (bc.id, bc.user.id, 1 if bc.is_enabled else 0, now_iso()),
    )


def save_connection_raw(conn_id: str, owner_id: int) -> None:
    """Save connection by raw IDs (when we don't have a BusinessConnection object)."""
    get_db().execute(
        """INSERT INTO connections(id, owner_id, enabled, created_at)
           VALUES(?,?,1,?)
           ON CONFLICT(id) DO UPDATE SET
               owner_id = excluded.owner_id,
               enabled  = 1""",
        (conn_id, owner_id, now_iso()),
    )


def owner_of(conn_id: str) -> int | None:
    row = get_db().execute(
        "SELECT owner_id FROM connections WHERE id=? AND enabled=1",
        (conn_id,),
    ).fetchone()
    return row[0] if row else None


async def ensure_connection(conn_id: str) -> int | None:
    """Get owner_id, auto-registering via API if not in DB."""
    owner = owner_of(conn_id)
    if owner:
        return owner
    # Connection not in DB (lost after deploy) — fetch from Telegram API
    try:
        bc = await bot.get_business_connection(conn_id)
        save_connection_raw(conn_id, bc.user.id)
        log.info("Auto-registered connection %s for user %s", conn_id, bc.user.id)
        return bc.user.id
    except Exception as e:
        log.warning("Failed to fetch connection %s: %s", conn_id, e)
        return None


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
        return "sticker", msg.sticker.file_id, {
            "emoji": msg.sticker.emoji,
            "set_name": msg.sticker.set_name,
            "is_animated": msg.sticker.is_animated,
            "is_video": msg.sticker.is_video,
        }
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


# Человекочитаемые названия типов
KIND_LABELS = {
    "text": "Текст",
    "photo": "Фото",
    "video": "Видео",
    "video_note": "Кружочек",
    "voice": "Голосовое",
    "audio": "Аудио",
    "animation": "GIF",
    "document": "Документ",
    "sticker": "Стикер",
    "contact": "Контакт",
    "location": "Геолокация",
}


def _display_name(msg: Message) -> str:
    u = msg.from_user
    if not u:
        return "Неизвестный"
    parts = [p for p in (u.first_name, u.last_name) if p]
    name = " ".join(parts) or "Без имени"
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
    # View-once / timer media: has_protected_content and/or has_media_spoiler
    is_timer_media = bool(msg.has_protected_content or msg.has_media_spoiler)
    protected = 1 if is_timer_media else 0
    get_db().execute(
        """INSERT OR REPLACE INTO messages
           (conn_id, chat_id, message_id, sender_id, sender_name,
            kind, text, caption, file_id, extra, protected, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            protected,
            now_iso(),
        ),
    )


def recall(conn_id: str, chat_id: int, message_id: int) -> dict | None:
    row = get_db().execute(
        """SELECT sender_id, sender_name, kind, text, caption, file_id, extra, protected
             FROM messages
            WHERE conn_id=? AND chat_id=? AND message_id=?""",
        (conn_id, chat_id, message_id),
    ).fetchone()
    if not row:
        return None
    return {
        "sender_id": row[0],
        "sender_name": row[1],
        "kind": row[2],
        "text": row[3],
        "caption": row[4],
        "file_id": row[5],
        "extra": json.loads(row[6]) if row[6] else {},
        "protected": bool(row[7]),
    }


def recall_by_chat(owner_id: int, chat_id: int, message_id: int) -> dict | None:
    """Recall a message by chat + message_id. No JOIN — works even if connections is empty."""
    row = get_db().execute(
        """SELECT sender_id, sender_name, kind, text, caption,
                  file_id, extra, protected
             FROM messages
            WHERE chat_id = ?
              AND message_id = ?
            LIMIT 1""",
        (chat_id, message_id),
    ).fetchone()
    if not row:
        return None
    return {
        "sender_id": row[0],
        "sender_name": row[1],
        "kind": row[2],
        "text": row[3],
        "caption": row[4],
        "file_id": row[5],
        "extra": json.loads(row[6]) if row[6] else {},
        "protected": bool(row[7]),
    }


def recall_batch(conn_id: str, chat_id: int, message_ids: list[int]) -> list[tuple[int, dict]]:
    """Recall multiple messages at once. Returns list of (message_id, data)."""
    if not message_ids:
        return []
    db = get_db()
    placeholders = ",".join("?" for _ in message_ids)
    rows = db.execute(
        f"""SELECT message_id, sender_id, sender_name, kind, text, caption, file_id, extra, protected
              FROM messages
             WHERE conn_id=? AND chat_id=? AND message_id IN ({placeholders})
             ORDER BY message_id ASC""",
        [conn_id, chat_id, *message_ids],
    ).fetchall()
    result = []
    for r in rows:
        result.append((r[0], {
            "sender_id": r[1],
            "sender_name": r[2],
            "kind": r[3],
            "text": r[4],
            "caption": r[5],
            "file_id": r[6],
            "extra": json.loads(r[7]) if r[7] else {},
            "protected": bool(r[8]),
        }))
    return result


# ========================= Resending =========================

async def _send_one(owner: int, item: dict, header: str) -> None:
    """Send a single cached message to the owner with nice formatting."""
    name = _safe(item.get("sender_name") or "Неизвестный")
    kind = item.get("kind") or "unknown"
    kind_label = KIND_LABELS.get(kind, kind)
    fid = item.get("file_id")
    cap = _safe(item.get("caption"))
    extra = item.get("extra") or {}

    # Build info header
    info = f"{header}\n<b>От:</b> {name}\n<b>Тип:</b> {kind_label}"

    try:
        if kind == "text":
            text = _safe(item.get("text") or "")
            await bot.send_message(owner, f"{info}\n\n{text}")

        elif kind == "photo":
            body = f"{info}\n\n{cap}" if cap else info
            await bot.send_photo(owner, fid, caption=body)

        elif kind == "video":
            body = f"{info}\n\n{cap}" if cap else info
            await bot.send_video(owner, fid, caption=body)

        elif kind == "animation":
            body = f"{info}\n\n{cap}" if cap else info
            await bot.send_animation(owner, fid, caption=body)

        elif kind == "voice":
            body = f"{info}\n\n{cap}" if cap else info
            await bot.send_voice(owner, fid, caption=body)

        elif kind == "audio":
            body = f"{info}\n\n{cap}" if cap else info
            await bot.send_audio(owner, fid, caption=body)

        elif kind == "document":
            doc_name = extra.get("name", "")
            doc_info = f"{info}"
            if doc_name:
                doc_info += f"\n<b>Файл:</b> {_safe(doc_name)}"
            body = f"{doc_info}\n\n{cap}" if cap else doc_info
            await bot.send_document(owner, fid, caption=body)

        elif kind == "sticker":
            emoji = extra.get("emoji", "")
            set_name = extra.get("set_name", "")
            sticker_info = info
            if emoji:
                sticker_info += f"\n<b>Эмодзи:</b> {emoji}"
            if set_name:
                sticker_info += f"\n<b>Набор:</b> {_safe(set_name)}"
            await bot.send_message(owner, sticker_info)
            await bot.send_sticker(owner, fid)

        elif kind == "video_note":
            await bot.send_message(owner, info)
            await bot.send_video_note(owner, fid)

        elif kind == "contact":
            phone = extra.get("phone", "")
            contact_name = extra.get("name", "")
            await bot.send_message(
                owner,
                f"{info}\n\n<b>Имя:</b> {_safe(contact_name)}\n"
                f"<b>Телефон:</b> {_safe(phone)}",
            )

        elif kind == "location":
            await bot.send_message(owner, info)
            await bot.send_location(owner, extra["lat"], extra["lon"])

        else:
            await bot.send_message(owner, f"{info}\n\n<i>[{_safe(kind)}]</i>")

    except Exception as e:
        log.warning("resend failed for %s: %s", kind, e)
        await bot.send_message(
            owner,
            f"{info}\n\n<i>Не удалось переслать ({e.__class__.__name__}).</i>",
        )


async def forward_deleted_batch(
    owner: int,
    items: list[tuple[int, dict]],
    chat_title: str | None,
) -> None:
    """Send a batch of deleted messages to the owner with a single header."""
    count = len(items)
    chat_info = f" в чате с <b>{_safe(chat_title)}</b>" if chat_title else ""

    if count == 1:
        _, item = items[0]
        await _send_one(owner, item, "\U0001f5d1 <b>Удалённое сообщение</b>" + chat_info)
        return

    header_text = (
        f"\U0001f5d1 <b>Удалено {count} сообщений</b>{chat_info}\n"
        f"{'─' * 20}"
    )
    await bot.send_message(owner, header_text)

    for i, (mid, item) in enumerate(items[:BATCH_DISPLAY_LIMIT], 1):
        await _send_one(owner, item, f"<b>[{i}/{count}]</b>")
        if i % 5 == 0:
            await asyncio.sleep(0.5)

    if count > BATCH_DISPLAY_LIMIT:
        await bot.send_message(
            owner,
            f"<i>...и ещё {count - BATCH_DISPLAY_LIMIT} сообщений "
            f"(показаны первые {BATCH_DISPLAY_LIMIT}).</i>",
        )


async def forward_cached(owner: int, item: dict, header: str) -> None:
    """Send a single cached message (for reaction/reply saves)."""
    await _send_one(owner, item, header)


# ========================= Business handlers =========================

@router.business_connection()
async def on_connect(bc: BusinessConnection) -> None:
    save_connection(bc)
    log.info("Business connection %s: user=%s enabled=%s", bc.id, bc.user.id, bc.is_enabled)
    try:
        if bc.is_enabled:
            await bot.send_message(
                bc.user.id,
                "\u2705 <b>Бот подключён</b>\n\n"
                "Теперь я слежу за твоими чатами в фоне.\n"
                "Если кто-то удалит сообщение \u2014 пришлю копию.\n\n"
                "\u23f3 Таймер-сообщение? Ответь или поставь реакцию \u2014 сохраню.\n\n"
                "/help \u2014 подробнее",
            )
        else:
            await bot.send_message(
                bc.user.id, "\U0001f6d1 Бот отключён."
            )
    except Exception as e:
        log.warning("notify on connect failed: %s", e)


@router.business_message()
async def on_business_message(msg: Message) -> None:
    conn_id = msg.business_connection_id or ""

    # Auto-register connection if missing (lost after deploy)
    owner = await ensure_connection(conn_id)

    remember(msg)
    log.info(
        "business_message: conn=%s chat=%s msg=%s from=%s kind=%s owner=%s "
        "protected=%s spoiler=%s has_media=%s",
        conn_id, msg.chat.id, msg.message_id,
        msg.from_user.id if msg.from_user else None,
        msg.content_type, owner,
        msg.has_protected_content, msg.has_media_spoiler,
        bool(msg.photo or msg.video or msg.animation or msg.video_note),
    )

    # Self-destruct rescue: owner replies to a message — resend the cached original
    if not owner or not msg.from_user or msg.from_user.id != owner:
        return
    if not msg.reply_to_message:
        return

    r = msg.reply_to_message
    log.info(
        "Owner replied to msg %s in chat %s (conn=%s), attempting rescue",
        r.message_id, msg.chat.id, conn_id,
    )

    # Try by conn_id first, then fallback to chat-based lookup
    cached = recall(conn_id, msg.chat.id, r.message_id)
    if not cached:
        cached = recall_by_chat(owner, msg.chat.id, r.message_id)

    if cached and cached.get("protected"):
        log.info("Rescue success (protected media): kind=%s", cached.get("kind"))
        await forward_cached(owner, cached, "\u23f3 <b>Таймер-медиа сохранено</b>")
    elif cached:
        log.info("Skipped rescue: message %s is not protected (not timer media)", r.message_id)
    else:
        log.info("Rescue failed: message %s not in cache", r.message_id)


@router.edited_business_message()
async def on_edited(msg: Message) -> None:
    if not msg.business_connection_id:
        return
    existing = recall(msg.business_connection_id, msg.chat.id, msg.message_id)
    if existing is None:
        remember(msg)


@router.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted) -> None:
    owner = await ensure_connection(event.business_connection_id)
    if not owner:
        return

    all_items = recall_batch(
        event.business_connection_id,
        event.chat.id,
        list(event.message_ids),
    )

    # Только сообщения от собеседника (не от владельца)
    items = [(mid, data) for mid, data in all_items if data.get("sender_id") != owner]

    log.info(
        "deleted_business_messages: conn=%s chat=%s total=%d from_contact=%d",
        event.business_connection_id, event.chat.id, len(event.message_ids), len(items),
    )

    if not items:
        return

    chat_title = None
    if event.chat:
        parts = [event.chat.first_name, event.chat.last_name]
        chat_title = " ".join(p for p in parts if p) or None

    await forward_deleted_batch(owner, items, chat_title)


@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated) -> None:
    """Rescue self-destruct media when the owner adds a reaction."""
    if not event.new_reaction or not event.user:
        return
    old_count = len(event.old_reaction) if event.old_reaction else 0
    if len(event.new_reaction) <= old_count:
        return

    user_id = event.user.id
    log.info(
        "message_reaction: chat=%s msg=%s user=%s reactions=%d->%d",
        event.chat.id, event.message_id, user_id,
        old_count, len(event.new_reaction),
    )

    # aiogram's MessageReactionUpdated doesn't have business_connection_id,
    # so we always look up by owner + chat + message_id
    cached = recall_by_chat(user_id, event.chat.id, event.message_id)

    if cached and cached.get("protected"):
        log.info("Reaction rescue success (protected): kind=%s", cached.get("kind"))
        await forward_cached(user_id, cached, "\u23f3 <b>Таймер-медиа сохранено</b>")
    elif cached:
        log.info("Skipped reaction rescue: msg %s not protected", event.message_id)
    else:
        log.info("Reaction rescue: msg %s not in cache for user %s", event.message_id, user_id)


# ========================= Private chat commands =========================

@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "\U0001f47b <b>Ghost Recovery Bot</b>\n\n"
        "Восстанавливаю удалённые сообщения из твоих чатов.\n\n"
        "\U0001f4ac Собеседник удалил текст, фото, видео, голосовое или стикер — "
        "ты получишь копию.\n\n"
        "\u23f3 Сообщение с таймером? Ответь на него или поставь реакцию — "
        "я сохраню до исчезновения.\n\n"
        "/help — как подключить"
    )


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "\U0001f4d6 <b>Как подключить</b>\n\n"
        "1. <b>Настройки</b> \u2192 <b>Telegram Business</b> \u2192 <b>Чат-боты</b>\n"
        "2. Найди этого бота и подключи\n"
        "3. Готово \u2014 бот работает в фоне\n\n"
        "<b>Что умеет:</b>\n"
        "\u2022 Удалённые сообщения \u2014 пришлю копию\n"
        "\u2022 Фото, видео, голосовые, стикеры, GIF, документы\n"
        "\u2022 Таймер-сообщения \u2014 ответь или поставь реакцию\n\n"
        "\U0001f512 Всё анонимно, данные не хранятся на серверах."
    )




# ========================= Entry =========================

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
        from aiogram.types import Update

        full_url = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        await bot.set_webhook(
            full_url,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=False,
        )
        log.info("Webhook set: %s", full_url)

        async def handle_webhook(request: web.Request) -> web.Response:
            try:
                data = await request.json()
                log.debug("Webhook update keys: %s", list(data.keys()))
                update = Update.model_validate(data, context={"bot": bot})
                await dp.feed_update(bot=bot, update=update)
            except Exception as e:
                log.error("Webhook handler error: %s — raw keys: %s", e, list(data.keys()) if 'data' in dir() else '?')
            return web.Response(text="ok")

        app.router.add_post(WEBHOOK_PATH, handle_webhook)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PORT)
        log.info("Listening on %s:%s", HOST, PORT)
        await site.start()
        await asyncio.Event().wait()
    else:
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
