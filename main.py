"""
Ghost Recovery Bot
------------------
Recovers messages your contacts delete from your Telegram Business chats.
Saves all message types: text, photo, video, voice, stickers, GIF, documents.

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
    try:
        get_db().execute(
            """INSERT OR REPLACE INTO messages
               (conn_id, chat_id, message_id, sender_id, sender_name,
                kind, text, caption, file_id, extra, protected, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
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
        # Верификация что записалось
        check = get_db().execute(
            "SELECT kind, sender_id FROM messages WHERE conn_id=? AND chat_id=? AND message_id=?",
            (msg.business_connection_id, msg.chat.id, msg.message_id),
        ).fetchone()
        if not check:
            log.error("DB WRITE FAILED: msg=%s not found after insert!", msg.message_id)
    except Exception as e:
        log.error("DB ERROR in remember(): msg=%s err=%s", msg.message_id, e)


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
    log.info("SENDING to %s: kind=%s file_id=%s text=%s", owner, kind, bool(fid), bool(item.get("text")))
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
                "\U0001f512 Твои данные в безопасности \u2014 бот не хранит "
                "переписки и не передаёт их третьим лицам.\n\n"
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

    sender_id = msg.from_user.id if msg.from_user else None
    kind, file_id, _ = _classify(msg)
    log.info(
        "SAVED msg=%s chat=%s from=%s kind=%s file_id=%s",
        msg.message_id, msg.chat.id, sender_id, kind,
        (file_id[:20] + "...") if file_id else None,
    )


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

    msg_ids = list(event.message_ids)
    all_items = recall_batch(
        event.business_connection_id,
        event.chat.id,
        msg_ids,
    )

    # Логируем что нашли и что нет
    found_ids = {mid for mid, _ in all_items}
    missing_ids = [mid for mid in msg_ids if mid not in found_ids]
    if missing_ids:
        log.warning("DELETED but NOT in DB: msg_ids=%s (sent before deploy?)", missing_ids)

    # Только сообщения от собеседника (не от владельца)
    items = [(mid, data) for mid, data in all_items if data.get("sender_id") != owner]
    skipped = [(mid, data) for mid, data in all_items if data.get("sender_id") == owner]

    log.info(
        "DELETED: chat=%s ids=%s found=%d missing=%d from_contact=%d skipped_own=%d owner=%s",
        event.chat.id, msg_ids, len(all_items), len(missing_ids),
        len(items), len(skipped), owner,
    )

    if not items:
        return

    chat_title = None
    if event.chat:
        parts = [event.chat.first_name, event.chat.last_name]
        chat_title = " ".join(p for p in parts if p) or None

    await forward_deleted_batch(owner, items, chat_title)




# ========================= Private chat commands =========================

@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "\U0001f47b <b>Ghost Recovery Bot</b>\n\n"
        "Восстанавливаю удалённые сообщения из твоих бизнес-чатов.\n\n"
        "\U0001f4ac Собеседник удалил текст, фото, видео, голосовое или стикер — "
        "ты получишь копию.\n\n"
        "\U0001f512 <b>Безопасность:</b>\n"
        "\u2022 Бот не хранит и не передаёт твои данные третьим лицам\n"
        "\u2022 Каждый пользователь видит только свои чаты\n"
        "\u2022 Переписки между пользователями не пересекаются\n\n"
        "Вопросы или проблемы \u2192 @qstaeg\n\n"
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
        "\u2022 Фото, видео, голосовые, стикеры, GIF, документы\n\n"
        "\U0001f512 Данные не хранятся на серверах и не передаются третьим лицам.\n\n"
        "Вопросы \u2192 @qstaeg"
    )




# ========================= Entry =========================

ALLOWED_UPDATES = [
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
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
