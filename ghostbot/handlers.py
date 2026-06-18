"""Telegram update handlers — the glue between Telegram and the other modules.

Layout mirrors the kinds of updates we receive:

* business connection lifecycle
* incoming / edited / deleted business messages
* owner commands in the bot's private chat (``/start``, ``/help`` …)

In-chat ``.commands`` (typed in the conversation with a contact) live in
:mod:`ghostbot.commands`; handlers just route to them.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    Message,
)

from .actions import (
    afk,
    bot_sent,
    delete_msgs,
    rescued,
    self_deleted,
    send_as_owner,
    was_sent_text,
)
from .commands import _angrify, _kindify, dispatch
from .config import RECALL_RETRIES, RECALL_RETRY_DELAY, SUPPORT_CONTACT, log
from .formatting import chat_title as build_chat_title
from .formatting import classify
from .sender import forward_deleted, forward_edited, send_recovered
from .storage import (
    banwords_on,
    count_for_owner,
    get_style,
    mute_until,
    owner_of,
    recall,
    recall_batch,
    remember,
    save_connection,
    text_has_banword,
    wipe_owner,
)

# Media kinds worth rescuing when the owner replies to a (possibly one-view) message.
_RESCUE_KINDS = {"photo", "video", "voice", "video_note", "animation"}

router = Router(name="ghost")


# ============================ Connection resolving ============================

async def ensure_connection(bot: Bot, conn_id: str) -> int | None:
    """Return the owner id for a connection, auto-registering if needed.

    After a redeploy the connections table may be empty while Telegram keeps
    sending updates; we recover the mapping from the API on demand.
    """
    if not conn_id:
        return None
    owner = owner_of(conn_id)
    if owner:
        return owner
    try:
        bc = await bot.get_business_connection(conn_id)
        save_connection(bc.id, bc.user.id, bool(bc.is_enabled))
        log.info("auto-registered connection %s → owner %s", conn_id, bc.user.id)
        return bc.user.id
    except Exception as e:
        log.warning("could not resolve connection %s: %s", conn_id, e)
        return None


def _is_muted(conn_id: str, chat_id: int) -> bool:
    return mute_until(conn_id, chat_id) is not False


async def _notify(bot: Bot, owner: int, text: str) -> None:
    try:
        await bot.send_message(owner, text)
    except Exception as e:
        log.warning("notify owner failed: %s", e)


# ============================ Business lifecycle ============================

@router.business_connection()
async def on_connect(bc: BusinessConnection, bot: Bot) -> None:
    save_connection(bc.id, bc.user.id, bool(bc.is_enabled))
    log.info("business connection %s: user=%s enabled=%s", bc.id, bc.user.id, bc.is_enabled)
    try:
        if bc.is_enabled:
            await bot.send_message(
                bc.user.id,
                "✅ <b>Бот подключён</b>\n\n"
                "Слежу за чатами в фоне:\n"
                "🗑 удалят — пришлю копию\n"
                "✏️ изменят — покажу было/стало\n"
                "👻 одноразовое медиа — сохраню\n\n"
                "А ещё — команды прямо в чате: набери <code>.help</code> в любом диалоге.\n\n"
                "🔒 Данные не передаются третьим лицам.\n\n"
                "/help — подробнее",
            )
        else:
            await bot.send_message(bc.user.id, "🛑 Бот отключён.")
    except Exception as e:
        log.warning("notify on connect failed: %s", e)


async def _maybe_rescue(bot: Bot, conn_id: str, msg: Message, owner: int) -> None:
    """If the owner replied to a contact's media message, resend a copy to DM.

    This powers the "reply to a one-view file to save it" flow: the owner swipes
    to reply (without opening) and writes anything; we deliver the cached media.
    """
    replied = msg.reply_to_message
    if not replied:
        return
    key = (conn_id, msg.chat.id, replied.message_id)
    if key in rescued:
        return
    data = recall(conn_id, msg.chat.id, replied.message_id)
    if not data or data.get("sender_id") == owner:
        return
    if data.get("kind") not in _RESCUE_KINDS:
        return
    rescued.add(key)
    await send_recovered(bot, owner, data, "⌛️ <b>Исчезающее медиа</b>")


@router.business_message()
async def on_business_message(msg: Message, bot: Bot) -> None:
    conn_id = msg.business_connection_id or ""
    owner = await ensure_connection(bot, conn_id)
    sender_id = msg.from_user.id if msg.from_user else None

    # Ignore the echo of messages the bot itself just sent into the chat.
    echo_key = (conn_id, msg.chat.id, msg.message_id)
    if echo_key in bot_sent:
        bot_sent.discard(echo_key)
        return

    # --- Owner branch -----------------------------------------------------
    if owner and sender_id == owner:
        text = msg.text or ""
        if text.startswith(".") and await dispatch(bot, msg, conn_id, owner):
            return  # it was an in-chat command

        # Reply to a (possibly disappearing) media → rescue it.
        if msg.reply_to_message:
            await _maybe_rescue(bot, conn_id, msg, owner)

        # Any normal message cancels AFK.
        if conn_id in afk:
            afk.pop(conn_id, None)
            await _notify(bot, owner, "☀️ AFK снят — ты снова в сети.")

        # Style rewrite: delete the original and resend it rewritten as the
        # owner. (Editing a user's own message over a business connection isn't
        # reliable, so we replace it instead.)
        style = get_style(conn_id)
        if style and msg.text and not text.startswith("."):
            # Skip the echo of a message we just sent (prevents a rewrite loop).
            if was_sent_text(conn_id, msg.text):
                return
            new_text = _angrify(msg.text) if style == "ang" else _kindify(msg.text)
            await delete_msgs(bot, conn_id, msg.chat.id, [msg.message_id])
            try:
                await send_as_owner(bot, conn_id, msg.chat.id, new_text)
            except Exception as e:
                log.warning("style resend failed: %s", e)
            return

        remember(msg)
        return

    # --- Contact branch ---------------------------------------------------
    if owner:
        # Mute: drop the contact's message silently.
        if _is_muted(conn_id, msg.chat.id):
            await delete_msgs(bot, conn_id, msg.chat.id, [msg.message_id])
            log.info("mute: deleted msg=%s in chat=%s", msg.message_id, msg.chat.id)
            return

        # Ban words: delete messages containing forbidden words.
        if banwords_on(conn_id) and text_has_banword(owner, msg.text or ""):
            await delete_msgs(bot, conn_id, msg.chat.id, [msg.message_id])
            log.info("banword: deleted msg=%s in chat=%s", msg.message_id, msg.chat.id)
            return

        # AFK: auto-reply once per chat.
        if conn_id in afk:
            state = afk[conn_id]
            if msg.chat.id not in state["seen"]:
                state["seen"].add(msg.chat.id)
                try:
                    await send_as_owner(bot, conn_id, msg.chat.id, state["reason"])
                except Exception as e:
                    log.warning("afk auto-reply failed: %s", e)

    remember(msg)
    kind, file_id, _ = classify(msg)
    log.info("saved msg=%s chat=%s from=%s kind=%s media=%s",
             msg.message_id, msg.chat.id, sender_id, kind, bool(file_id))


@router.edited_business_message()
async def on_edited(msg: Message, bot: Bot) -> None:
    conn_id = msg.business_connection_id or ""
    owner = await ensure_connection(bot, conn_id)
    sender_id = msg.from_user.id if msg.from_user else None

    old = recall(conn_id, msg.chat.id, msg.message_id)
    remember(msg, edited=True)  # keep the latest version for future recovery

    if not owner or sender_id == owner:
        return  # never report the owner's own edits
    if _is_muted(conn_id, msg.chat.id):
        return

    # Skip no-op edits (e.g. Telegram re-sends when only a link preview changes).
    if old and old.get("text") == msg.text and old.get("caption") == msg.caption:
        return

    new = recall(conn_id, msg.chat.id, msg.message_id) or {}
    title = build_chat_title(msg.chat.first_name, msg.chat.last_name)
    await forward_edited(bot, owner, old, new, title)


@router.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted, bot: Bot) -> None:
    conn_id = event.business_connection_id
    owner = await ensure_connection(bot, conn_id)
    if not owner:
        return

    # Drop ids the bot deleted itself (mute / commands / self-destruct).
    msg_ids: list[int] = []
    for mid in event.message_ids:
        key = (conn_id, event.chat.id, mid)
        if key in self_deleted:
            self_deleted.discard(key)
        else:
            msg_ids.append(mid)
    if not msg_ids:
        return

    # Race guard: the matching business_message update may still be arriving.
    all_items: list[tuple[int, dict]] = []
    for _ in range(RECALL_RETRIES):
        all_items = recall_batch(conn_id, event.chat.id, msg_ids)
        if len(all_items) == len(msg_ids):
            break
        await asyncio.sleep(RECALL_RETRY_DELAY)

    found_ids = {mid for mid, _ in all_items}
    missing = [mid for mid in msg_ids if mid not in found_ids]
    if missing:
        log.warning("deleted but never stored (sent before bot started?): %s", missing)

    # Only the contact's messages — never echo the owner's own deletions.
    items = [(mid, d) for mid, d in all_items if d.get("sender_id") != owner]
    log.info("deleted chat=%s requested=%d found=%d from_contact=%d owner=%s",
             event.chat.id, len(msg_ids), len(all_items), len(items), owner)

    if not items:
        return

    title = build_chat_title(event.chat.first_name, event.chat.last_name)
    await forward_deleted(bot, owner, items, title)


# ============================ Private chat commands ============================

@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "👻 <b>Ghost Recovery Bot</b>\n\n"
        "Твой личный агент в Telegram. Возвращаю спрятанное и помогаю в переписке:\n\n"
        "🗑 <b>Удалённое</b> — пришлю копию\n"
        "✏️ <b>Изменённое</b> — покажу, что было и что стало\n"
        "👻 <b>Одноразовое медиа</b> — сохраню до того, как исчезнет\n\n"
        "⚡️ <b>Команды прямо в чате</b> с собеседником (он их не видит):\n"
        "<code>.type</code>, <code>.bomb</code>, <code>.mock</code>, <code>.8ball</code>, "
        "<code>.ang</code>/<code>.kind</code>, <code>.bw</code>, <code>.afk</code>…\n"
        "⌛️ Реплай на одноразовое медиа (не открывая) — сохраню копию.\n"
        "Полный список — набери <code>.help</code> в любом диалоге.\n\n"
        "🔒 <b>Приватность:</b> данные только твои.\n\n"
        f"Вопросы → {SUPPORT_CONTACT}\n\n"
        "/help — как подключить"
    )


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "📖 <b>Как подключить</b>\n\n"
        "1. <b>Настройки</b> → <b>Telegram для бизнеса</b> → <b>Чат-боты</b>\n"
        "   (или <b>Автоматизация чатов</b>)\n"
        "2. Найди этого бота и добавь\n"
        "3. Разреши читать и управлять сообщениями\n"
        "4. Готово — я работаю в фоне\n\n"
        "<b>Команды в личке с ботом:</b>\n"
        "/status — статус и сколько сообщений в кэше\n"
        "/wipe — стереть всё, что я сохранил\n\n"
        "<b>Команды прямо в чате с собеседником:</b>\n"
        "набери <code>.help</code> — пришлю весь список\n\n"
        "🔒 Данные не передаются третьим лицам.\n\n"
        f"Вопросы → {SUPPORT_CONTACT}"
    )


@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    cached = count_for_owner(msg.from_user.id)
    connected = owner_has_connection(msg.from_user.id)
    if connected or cached:
        await msg.answer(
            "📊 <b>Статус</b>\n\n"
            f"Сообщений в кэше: <b>{cached}</b>\n"
            "Подключение активно, слежу за чатами. ✅"
        )
    else:
        await msg.answer("📊 <b>Статус</b>\n\nПока нет подключения. Открой /help.")


@router.message(Command("wipe"))
async def cmd_wipe(msg: Message) -> None:
    removed = wipe_owner(msg.from_user.id)
    await msg.answer(f"🧹 Готово. Удалено сообщений: <b>{removed}</b>.")


def owner_has_connection(owner_id: int) -> bool:
    from .storage import get_db
    row = get_db().execute(
        "SELECT 1 FROM connections WHERE owner_id=? AND enabled=1 LIMIT 1", (owner_id,)
    ).fetchone()
    return bool(row)
