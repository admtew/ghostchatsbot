"""Resilient delivery of recovered messages to the owner.

Two reliability rules drive this module:

1. **Never lose half a notification.** Where Telegram allows it, the header and
   the media go out as a *single* message (caption). For kinds that can't carry
   a caption (stickers, video notes, locations) we send the text first, then
   the media, and treat each as independently retryable.
2. **Survive flood limits.** Every outgoing call goes through :func:`_call`,
   which retries on ``TelegramRetryAfter`` (HTTP 429) instead of dropping the
   message.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from .config import BATCH_DISPLAY_LIMIT, SEND_RETRIES, log
from .formatting import kind_label, safe, trim_caption

# Map kinds to the Bot method that sends them as a file.
_MEDIA_SENDERS = {
    "photo": "send_photo",
    "video": "send_video",
    "animation": "send_animation",
    "voice": "send_voice",
    "audio": "send_audio",
    "document": "send_document",
    "video_note": "send_video_note",
    "sticker": "send_sticker",
}
# Kinds whose sender accepts a ``caption`` argument.
_CAPTIONABLE = {"photo", "video", "animation", "voice", "audio", "document"}


async def _call(factory: Callable[[], Awaitable], *, what: str) -> bool:
    """Run a send coroutine with flood-retry. Returns True on success."""
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            await factory()
            return True
        except TelegramRetryAfter as e:
            wait = e.retry_after + 0.5
            log.warning("flood limit on %s, retry in %.1fs (%d/%d)",
                        what, wait, attempt, SEND_RETRIES)
            await asyncio.sleep(wait)
        except TelegramForbiddenError:
            log.warning("owner blocked the bot, dropping %s", what)
            return False
        except Exception as e:
            log.warning("send %s failed (%d/%d): %s",
                        what, attempt, SEND_RETRIES, e)
            # Transient network hiccup — brief backoff then retry.
            await asyncio.sleep(0.5 * attempt)
    log.error("giving up on %s after %d attempts", what, SEND_RETRIES)
    return False


def _sender_link(item: dict) -> str:
    """Sender name as a clickable mention when we know the user id."""
    name = safe(item.get("sender_name") or "Неизвестный")
    sid = item.get("sender_id")
    return f'<a href="tg://user?id={sid}">{name}</a>' if sid else name


def _info_header(header: str, item: dict) -> str:
    """Build the text block shown above/with every recovered message."""
    kind = item.get("kind") or "unknown"
    edited = " · <i>ред.</i>" if item.get("edited") else ""
    return f"{header}\n<b>От:</b> {_sender_link(item)}\n<b>Тип:</b> {kind_label(kind)}{edited}"


async def send_recovered(bot: Bot, owner: int, item: dict, header: str) -> None:
    """Send one recovered message to the owner with full context."""
    kind = item.get("kind") or "unknown"
    fid = item.get("file_id")
    cap = safe(item.get("caption"))
    extra = item.get("extra") or {}
    info = _info_header(header, item)

    log.info("resend → %s: kind=%s media=%s", owner, kind, bool(fid))

    # --- Text -------------------------------------------------------------
    if kind == "text":
        body = safe(item.get("text") or "")
        await _call(lambda: bot.send_message(owner, f"{info}\n\n{body}"), what="text")
        return

    # --- Captionable media: one message carries everything ----------------
    if kind in _CAPTIONABLE and fid:
        extra_line = ""
        if kind == "document" and extra.get("name"):
            extra_line = f"\n<b>Файл:</b> {safe(extra['name'])}"
        caption = trim_caption(f"{info}{extra_line}\n\n{cap}" if cap else f"{info}{extra_line}")
        send = getattr(bot, _MEDIA_SENDERS[kind])
        ok = await _call(lambda: send(owner, fid, caption=caption), what=kind)
        if not ok:
            # Last resort: media without caption + header separately.
            await _call(lambda: send(owner, fid), what=f"{kind}/bare")
            await _call(lambda: bot.send_message(owner, caption), what=f"{kind}/cap")
        return

    # --- Sticker: text first, then the sticker ----------------------------
    if kind == "sticker" and fid:
        emoji = extra.get("emoji", "")
        set_name = extra.get("set_name", "")
        body = info
        if emoji:
            body += f"\n<b>Эмодзи:</b> {emoji}"
        if set_name:
            body += f"\n<b>Набор:</b> {safe(set_name)}"
        await _call(lambda: bot.send_message(owner, body), what="sticker/info")
        await _call(lambda: bot.send_sticker(owner, fid), what="sticker")
        return

    # --- Video note: round video can't carry a caption --------------------
    if kind == "video_note" and fid:
        await _call(lambda: bot.send_message(owner, info), what="video_note/info")
        await _call(lambda: bot.send_video_note(owner, fid), what="video_note")
        return

    # --- Contact ----------------------------------------------------------
    if kind == "contact":
        body = (f"{info}\n\n<b>Имя:</b> {safe(extra.get('name', ''))}\n"
                f"<b>Телефон:</b> {safe(extra.get('phone', ''))}")
        await _call(lambda: bot.send_message(owner, body), what="contact")
        return

    # --- Location ---------------------------------------------------------
    if kind == "location" and "lat" in extra:
        await _call(lambda: bot.send_message(owner, info), what="location/info")
        await _call(
            lambda: bot.send_location(owner, latitude=extra["lat"], longitude=extra["lon"]),
            what="location",
        )
        return

    # --- Poll -------------------------------------------------------------
    if kind == "poll":
        q = safe(extra.get("question", ""))
        opts = "\n".join(f"• {safe(o)}" for o in extra.get("options", []))
        await _call(lambda: bot.send_message(owner, f"{info}\n\n<b>{q}</b>\n{opts}"),
                    what="poll")
        return

    # --- Anything else: at least tell the owner something happened --------
    body = info
    if item.get("text"):
        body += f"\n\n{safe(item['text'])}"
    elif fid:
        body += "\n\n<i>(медиа не удалось восстановить)</i>"
    await _call(lambda: bot.send_message(owner, body), what="fallback")


async def forward_deleted(
    bot: Bot,
    owner: int,
    items: list[tuple[int, dict]],
    chat_title: str | None,
) -> None:
    """Forward a batch of deleted messages under a single header."""
    count = len(items)
    where = f" в чате с <b>{safe(chat_title)}</b>" if chat_title else ""

    if count == 1:
        _, item = items[0]
        await send_recovered(bot, owner, item, "🗑 <b>Удалённое сообщение</b>" + where)
        return

    await _call(
        lambda: bot.send_message(
            owner,
            f"🗑 <b>Удалено {count} сообщений</b>{where}\n{'─' * 18}",
        ),
        what="batch/header",
    )

    for i, (_mid, item) in enumerate(items[:BATCH_DISPLAY_LIMIT], 1):
        await send_recovered(bot, owner, item, f"<b>[{i}/{count}]</b>")
        if i % 4 == 0:
            await asyncio.sleep(0.4)  # be gentle with the flood limiter

    if count > BATCH_DISPLAY_LIMIT:
        await _call(
            lambda: bot.send_message(
                owner,
                f"<i>…и ещё {count - BATCH_DISPLAY_LIMIT} "
                f"(показаны первые {BATCH_DISPLAY_LIMIT}).</i>",
            ),
            what="batch/more",
        )


async def forward_edited(
    bot: Bot,
    owner: int,
    old: dict | None,
    new: dict,
    chat_title: str | None,
) -> None:
    """Notify the owner that a message was edited, showing before → after."""
    where = f" в чате с <b>{safe(chat_title)}</b>" if chat_title else ""
    header = f"✏️ <b>Сообщение изменено</b>{where}\n<b>От:</b> {_sender_link(new)}"

    old_body = (old or {}).get("text") or (old or {}).get("caption") or ""
    new_body = new.get("text") or new.get("caption") or ""

    # Text/caption edits: the most useful case — show the diff.
    if old_body or new_body:
        body = (
            f"{header}\n\n"
            f"<b>Было:</b>\n{safe(old_body) or '<i>—</i>'}\n\n"
            f"<b>Стало:</b>\n{safe(new_body) or '<i>—</i>'}"
        )
        await _call(lambda: bot.send_message(owner, trim_caption(body, 4000)),
                    what="edit/text")
        return

    # Media edited without text — resend the new version for reference.
    await send_recovered(bot, owner, new, header)
