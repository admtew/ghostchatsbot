"""Low-level actions performed *inside* a business chat, on the owner's behalf.

Everything here goes through ``business_connection_id`` so the bot acts as the
owner: it can send, edit and delete messages right in the conversation with a
contact. Kept dependency-free (only aiogram + config) so both the command layer
and the handlers can import it without cycles.
"""

from __future__ import annotations

from collections import deque

from aiogram import Bot

from .config import log

# Messages the bot deleted itself (commands, mute, self-destruct). The delete
# handler checks this so it never reports them back as "deleted by the contact".
self_deleted: set[tuple[str, int, int]] = set()

# AFK auto-reply state, per connection: conn_id -> {"reason": str, "seen": set[chat_id]}
afk: dict[str, dict] = {}

# Disappearing-media messages we've already rescued, so a second reply to the
# same message doesn't spam the owner. Keys: (conn_id, chat_id, message_id).
rescued: set[tuple[str, int, int]] = set()

# Messages the bot itself sent into a chat (commands, style rewrites, AFK).
# Telegram echoes them back as business_message updates; we skip those so they
# aren't re-processed (which would loop style rewriting) or stored as the owner.
bot_sent: set[tuple[str, int, int]] = set()


def mark_sent(conn_id: str, chat_id: int, message_id: int) -> None:
    bot_sent.add((conn_id, chat_id, message_id))
    if len(bot_sent) > 3000:
        bot_sent.pop()


# Text of recent bot-sent messages per connection. Telegram may echo a sent
# message back with a *different* message_id than the API returned, so we also
# recognise our own messages by content — this is what stops style-rewrite loops.
_recent_text: dict[str, deque] = {}


def note_sent_text(conn_id: str, text: str) -> None:
    dq = _recent_text.setdefault(conn_id, deque(maxlen=30))
    dq.append(text)


def was_sent_text(conn_id: str, text: str) -> bool:
    """True (consuming the entry) if this text is the echo of a message we sent."""
    dq = _recent_text.get(conn_id)
    if dq and text in dq:
        try:
            dq.remove(text)
        except ValueError:
            pass
        return True
    return False


async def send_as_owner(bot: Bot, conn_id: str, chat_id: int, text: str, **kw):
    """Send a message into a chat as the owner, remembering it to ignore the echo."""
    m = await bot.send_message(chat_id, text, business_connection_id=conn_id, **kw)
    mark_sent(conn_id, chat_id, m.message_id)
    note_sent_text(conn_id, text)
    return m


async def delete_msgs(
    bot: Bot,
    conn_id: str,
    chat_id: int,
    message_ids: list[int],
    *,
    silent: bool = True,
) -> bool:
    """Delete messages in a business chat. ``silent`` marks them so the delete
    handler won't echo them back to the owner as a recovered message."""
    if not message_ids:
        return False
    try:
        await bot.delete_business_messages(
            business_connection_id=conn_id, message_ids=message_ids
        )
        if silent:
            for mid in message_ids:
                self_deleted.add((conn_id, chat_id, mid))
        return True
    except Exception as e:
        log.warning("delete_msgs failed (chat=%s ids=%s): %s", chat_id, message_ids, e)
        return False
