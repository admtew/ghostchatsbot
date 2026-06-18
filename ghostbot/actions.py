"""Low-level actions performed *inside* a business chat, on the owner's behalf.

Everything here goes through ``business_connection_id`` so the bot acts as the
owner: it can send, edit and delete messages right in the conversation with a
contact. Kept dependency-free (only aiogram + config) so both the command layer
and the handlers can import it without cycles.
"""

from __future__ import annotations

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


async def send_as_owner(bot: Bot, conn_id: str, chat_id: int, text: str, **kw):
    """Send a message into a chat as the owner, remembering its id to ignore the echo."""
    m = await bot.send_message(chat_id, text, business_connection_id=conn_id, **kw)
    mark_sent(conn_id, chat_id, m.message_id)
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
