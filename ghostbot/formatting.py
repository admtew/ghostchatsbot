"""Pure presentation helpers: classify messages, escape text, build headers.

Nothing here talks to Telegram or the database — it only transforms data into
strings. That makes the look-and-feel of notifications easy to tweak in one
place.
"""

from __future__ import annotations

import html

from aiogram.types import Message

# Human-readable names per message kind.
KIND_LABELS: dict[str, str] = {
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
    "poll": "Опрос",
    "dice": "Кубик",
}

# Emoji shown next to each kind, purely cosmetic.
KIND_ICONS: dict[str, str] = {
    "text": "💬",
    "photo": "🖼",
    "video": "🎬",
    "video_note": "⭕️",
    "voice": "🎤",
    "audio": "🎵",
    "animation": "🎞",
    "document": "📄",
    "sticker": "🩷",
    "contact": "👤",
    "location": "📍",
    "poll": "📊",
    "dice": "🎲",
}


def classify(msg: Message) -> tuple[str, str | None, dict]:
    """Return ``(kind, file_id, extra)`` describing a message's payload.

    ``file_id`` is the best re-sendable id (largest photo size, etc.) or
    ``None`` for payloads without a file. ``extra`` holds metadata we need to
    rebuild the notification later.
    """
    if msg.photo:
        return "photo", msg.photo[-1].file_id, {}
    if msg.video:
        return "video", msg.video.file_id, {"duration": msg.video.duration}
    if msg.video_note:
        return "video_note", msg.video_note.file_id, {
            "duration": msg.video_note.duration,
            "length": msg.video_note.length,
        }
    if msg.voice:
        return "voice", msg.voice.file_id, {"duration": msg.voice.duration}
    if msg.audio:
        return "audio", msg.audio.file_id, {
            "duration": msg.audio.duration,
            "title": msg.audio.title,
            "performer": msg.audio.performer,
        }
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
    if msg.poll:
        return "poll", None, {
            "question": msg.poll.question,
            "options": [o.text for o in msg.poll.options],
        }
    if msg.dice:
        return "dice", None, {"emoji": msg.dice.emoji, "value": msg.dice.value}
    if msg.text:
        return "text", None, {}
    return msg.content_type or "unknown", None, {}


def safe(text: str | None) -> str:
    """Escape HTML so user content can't break our markup."""
    return html.escape(text) if text else ""


def display_name(msg: Message) -> str:
    """Readable sender name, e.g. ``Иван Петров (@ivan)``."""
    u = msg.from_user
    if not u:
        return "Неизвестный"
    parts = [p for p in (u.first_name, u.last_name) if p]
    name = " ".join(parts) or "Без имени"
    if u.username:
        name = f"{name} (@{u.username})"
    return name


def chat_title(first_name: str | None, last_name: str | None) -> str | None:
    """Build a chat title from the chat's first/last name fields."""
    title = " ".join(p for p in (first_name, last_name) if p)
    return title or None


def kind_label(kind: str) -> str:
    icon = KIND_ICONS.get(kind, "")
    label = KIND_LABELS.get(kind, kind)
    return f"{icon} {label}".strip()


def trim_caption(text: str, limit: int = 1024) -> str:
    """Trim to Telegram's caption limit without splitting an HTML entity badly."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
