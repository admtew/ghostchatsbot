"""In-chat command framework: type ``.something`` right in the conversation.

The owner types a ``.command`` in a chat with a contact. The bot deletes that
message instantly (the contact never sees it) and runs the handler on the
owner's behalf via the business connection.

Adding a new command is one decorated function::

    @command("hello", help="Поздороваться", category="Развлечения")
    async def cmd_hello(ctx: Ctx) -> None:
        await ctx.send("Привет!")

That's it — it shows up in ``.help`` automatically.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.types import Message

from datetime import datetime, timedelta, timezone

from .actions import afk, delete_msgs, mark_sent
from .config import TZ_OFFSET, log
from .formatting import safe
from .storage import (
    add_ban_word,
    add_reminder,
    clear_mute,
    del_ban_word,
    list_ban_words,
    set_banwords,
    set_mute,
    set_style,
)

# ============================ Registry ============================


@dataclass
class Ctx:
    """Everything a command needs, plus convenience actions."""

    bot: Bot
    msg: Message          # the owner's command message (already deleted)
    conn_id: str
    owner: int
    arg: str              # raw text after the command word

    @property
    def args(self) -> list[str]:
        return self.arg.split()

    @property
    def chat_id(self) -> int:
        return self.msg.chat.id

    @property
    def reply(self) -> Message | None:
        return self.msg.reply_to_message

    async def send(self, text: str, **kw) -> Message:
        """Send a message into the chat as the owner."""
        m = await self.bot.send_message(
            self.chat_id, text, business_connection_id=self.conn_id, **kw
        )
        mark_sent(self.conn_id, self.chat_id, m.message_id)
        return m

    async def edit(self, message_id: int, text: str, **kw) -> None:
        await self.bot.edit_message_text(
            text,
            business_connection_id=self.conn_id,
            chat_id=self.chat_id,
            message_id=message_id,
            **kw,
        )

    async def delete(self, *message_ids: int) -> None:
        await delete_msgs(self.bot, self.conn_id, self.chat_id, list(message_ids))

    async def notify(self, text: str) -> None:
        """Send a private note to the owner's DM with the bot."""
        try:
            await self.bot.send_message(self.owner, text)
        except Exception as e:
            log.warning("ctx.notify failed: %s", e)


Handler = Callable[[Ctx], Awaitable[None]]


@dataclass
class Command:
    names: tuple[str, ...]
    handler: Handler
    help: str
    category: str


COMMANDS: dict[str, Command] = {}
_REGISTERED: list[Command] = []


def command(*names: str, help: str = "", category: str = "Прочее") -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        cmd = Command(names, fn, help, category)
        _REGISTERED.append(cmd)
        for n in names:
            COMMANDS[n] = cmd
        return fn

    return deco


async def dispatch(bot: Bot, msg: Message, conn_id: str, owner: int) -> bool:
    """Run an in-chat command if the message is one. Returns True if handled."""
    text = (msg.text or "").strip()
    if not text.startswith("."):
        return False
    # Split off the command word on the first whitespace (space OR newline), so
    # multi-line commands like .table work: ".table\nA | B\nC | D".
    parts = text[1:].split(maxsplit=1)
    if not parts:
        return False
    head = parts[0].lower()
    cmd = COMMANDS.get(head)
    if not cmd:
        return False
    rest = parts[1] if len(parts) > 1 else ""

    # Hide the command from the contact before doing anything else.
    await delete_msgs(bot, conn_id, msg.chat.id, [msg.message_id])

    ctx = Ctx(bot, msg, conn_id, owner, rest.strip())
    try:
        await cmd.handler(ctx)
    except Exception as e:
        log.warning("command .%s failed: %s", head, e)
        await ctx.notify(f"⚠️ <code>.{head}</code> не выполнилась: {e}")
    return True


# ============================ Text helpers ============================

_FLIP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.,?!'\"()[]{}<>",
    "ɐqɔpǝɟƃɥᴉɾʞlɯuodbɹsʇnʌʍxʎz∀ᗺƆᗡƎℲ⅁HIſʞ˥WNOԀΌᴚS⊥∩ΛMX⅄Z˙'¿¡,„)(][}{><",
)

_8BALL = [
    # да
    "да, и не еби мозги",
    "конечно да, хули нет",
    "блять ну разумеется да",
    "100% да, мамой клянусь",
    "да, звёзды сказали го",
    "ну а то, ясен пень да",
    "однозначно да 💯",
    "да, делай уже не тупи",
    "да, сам бог велел",
    "да, но потом не ной",
    "айда, чё ты как не родной",
    # нет
    "нет нахуй",
    "нет блять, ты совсем ебанулся?",
    "ни за что, даже не мечтай",
    "нет, и точка",
    "абсолютно нет, дурка по тебе плачет",
    "нет, звёзды ржут с тебя",
    "не, забей короче",
    "нет, это днище-идея",
    "лучше не надо, чувак",
    "нет, я серьёзно, остынь",
    # нейтрально / угар
    "я бы не стал, но ты дурак — давай",
    "хз честно, спроси у мамы",
    "50 на 50, кидай монетку",
    "шар думает... шар устал, отъебись",
    "может да, может нет, может дождик может снег",
    "сегодня не твой день, попробуй завтра",
    "вселенная говорит «а пофиг, делай»",
    "знаки говорят да, но знаки бухие",
    "спроси позже, я обедаю",
    "результат туманный, как твоё будущее",
    "да... но это не точно",
    "карма против, но когда тебя это останавливало",
]

# --- Outgoing-message rewriting (rule-based, self-contained) ------------------
_ANG_PREFIX = ["слушай сюда уебок,", "блять,", "эй дебил,", "ну чё,",
               "сука,", "ало гандон,", "значит так,"]
_ANG_INSERT = ["блять", "нахуй", "сука", "ёпта", "бля"]
_ANG_SUFFIX = ["сука блять", "нахуй", "ёбаный в рот", "тварь",
               "уебище", "пиздец", "понял да"]

_KIND_PREFIX = ["Приветик", "Солнышко,", "Зайка,", "Дорогуша,", "Лапочка,", "Котик,"]
_KIND_SUFFIX = ["обнимаю 🤗", "ты лучик 🌞", "люблю 💕", "хорошего дня 🌸",
                "береги себя 🌷", "ты чудо ✨"]
_KIND_EMOJI = ["🌸", "💕", "🥰", "✨", "🌷", "🤗", "😊", "🌞", "💗"]


def _angrify(text: str) -> str:
    words = text.split()
    out: list[str] = []
    for w in words:
        out.append(w)
        if random.random() < 0.3:
            out.append(random.choice(_ANG_INSERT))
    s = " ".join(out)
    if random.random() < 0.7:
        s = f"{random.choice(_ANG_PREFIX)} {s}"
    if random.random() < 0.7:
        s = f"{s} {random.choice(_ANG_SUFFIX)}"
    return s


def _kindify(text: str) -> str:
    s = text
    if random.random() < 0.7:
        s = f"{random.choice(_KIND_PREFIX)} {s}"
    s = f"{s} {random.choice(_KIND_EMOJI)}"
    if random.random() < 0.6:
        s = f"{s} {random.choice(_KIND_SUFFIX)}"
    return s


def _mock(text: str) -> str:
    out = []
    upper = False
    for ch in text:
        out.append(ch.upper() if upper else ch.lower())
        if ch.isalpha():
            upper = not upper
    return "".join(out)


def _glitch(text: str) -> str:
    marks = "̧̖̗̀́̂̃̈҉"
    return "".join(ch + "".join(random.choice(marks) for _ in range(random.randint(1, 3)))
                    for ch in text)


def _render_table(arg: str) -> str | None:
    """Build a monospace bordered table from rows (newline or ';') × cells ('|').

    Returns an HTML <pre> block, or None if there's nothing to render.
    """
    raw_rows = [r for r in re.split(r"[\n;]", arg) if r.strip()]
    if not raw_rows:
        return None
    rows = [[c.strip()[:40] for c in r.split("|")] for r in raw_rows][:20]
    ncols = min(max(len(r) for r in rows), 6)
    rows = [(r + [""] * ncols)[:ncols] for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(ncols)]

    def hline(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def line(cells: list[str]) -> str:
        return "│" + "│".join(f" {c.ljust(w)} " for c, w in zip(cells, widths)) + "│"

    out = [hline("┌", "┬", "┐")]
    for i, r in enumerate(rows):
        out.append(line(r))
        out.append(hline("├", "┼", "┤") if i < len(rows) - 1 else hline("└", "┴", "┘"))
    return f"<pre>{safe(chr(10).join(out))}</pre>"


# ============================ Entertainment ============================

@command("type", help="Печатает текст по буквам: .type привет", category="Развлечения")
async def cmd_type(ctx: Ctx) -> None:
    text = (ctx.arg or (ctx.reply.text if ctx.reply else "")).strip()
    if not text:
        return
    text = text[:60]  # keep the edit-rate sane
    sent = await ctx.send("▌")
    acc = ""
    for ch in text:
        acc += ch
        try:
            await ctx.edit(sent.message_id, acc + "▌")
        except Exception:
            pass
        await asyncio.sleep(0.13)
    try:
        await ctx.edit(sent.message_id, acc)
    except Exception:
        pass


@command("bomb", help="Отсчёт и взрыв: .bomb 3 сюрприз", category="Развлечения")
async def cmd_bomb(ctx: Ctx) -> None:
    args = ctx.args
    n, text = 3, ctx.arg
    if args and args[0].isdigit():
        n = min(int(args[0]), 10)
        text = ctx.arg.split(maxsplit=1)[1] if len(args) > 1 else ""
    sent = await ctx.send(f"💣 {n}")
    for i in range(n - 1, -1, -1):
        await asyncio.sleep(1)
        try:
            await ctx.edit(sent.message_id, f"💣 {i}" if i else "💥")
        except Exception:
            pass
    if text:
        await asyncio.sleep(0.6)
        try:
            await ctx.edit(sent.message_id, f"💥 {text}")
        except Exception:
            pass


@command("mock", help="сПоНжБоБ-РеГиСтР: .mock текст", category="Развлечения")
async def cmd_mock(ctx: Ctx) -> None:
    text = ctx.arg or (ctx.reply.text if ctx.reply else "")
    if text:
        await ctx.send(_mock(text))


@command("flip", help="Перевернуть текст: .flip hello", category="Развлечения")
async def cmd_flip(ctx: Ctx) -> None:
    text = ctx.arg or (ctx.reply.text if ctx.reply else "")
    if text:
        await ctx.send(text[::-1].translate(_FLIP))


@command("glitch", help="Глитч-текст: .glitch текст", category="Развлечения")
async def cmd_glitch(ctx: Ctx) -> None:
    text = ctx.arg or (ctx.reply.text if ctx.reply else "")
    if text:
        await ctx.send(_glitch(text[:40]))


@command("8ball", "ball", help="Магический шар: .8ball стоит ли?", category="Развлечения")
async def cmd_8ball(ctx: Ctx) -> None:
    q = f"❓ {ctx.arg}\n" if ctx.arg else ""
    await ctx.send(f"{q}🎱 {random.choice(_8BALL)}")


@command("roll", help="Случайное число: .roll или .roll 100", category="Развлечения")
async def cmd_roll(ctx: Ctx) -> None:
    top = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 6
    await ctx.send(f"🎲 {random.randint(1, max(top, 1))}")


@command("coin", help="Орёл или решка", category="Развлечения")
async def cmd_coin(ctx: Ctx) -> None:
    await ctx.send(random.choice(["🦅 Орёл", "🪙 Решка"]))


# ============================ Modes ============================

@command("ang", help="Злой режим: твои сообщения станут грубыми", category="Режимы")
async def cmd_ang(ctx: Ctx) -> None:
    set_style(ctx.conn_id, "ang")
    await ctx.notify(
        "😈 <b>Злой режим включён.</b> Твои сообщения буду переписывать грубо.\n"
        "<code>.unang</code> — выключить."
    )


@command("unang", help="Выключить злой режим", category="Режимы")
async def cmd_unang(ctx: Ctx) -> None:
    set_style(ctx.conn_id, None)
    await ctx.notify("😇 Злой режим выключен.")


@command("kind", help="Добрый режим: мягко и со смайликами", category="Режимы")
async def cmd_kind(ctx: Ctx) -> None:
    set_style(ctx.conn_id, "kind")
    await ctx.notify(
        "🌸 <b>Добрый режим включён.</b> Твои сообщения станут милыми.\n"
        "<code>.unkind</code> — выключить."
    )


@command("unkind", help="Выключить добрый режим", category="Режимы")
async def cmd_unkind(ctx: Ctx) -> None:
    set_style(ctx.conn_id, None)
    await ctx.notify("🙂 Добрый режим выключен.")


@command("bw", help="Бан-слова: .bw add спам / .bw list / .bw off", category="Режимы")
async def cmd_bw(ctx: Ctx) -> None:
    args = ctx.args
    if not args:
        set_banwords(ctx.conn_id, True)
        await ctx.notify(
            "🚫 <b>Бан-слова включены.</b> Сообщения собеседника с этими словами удаляю.\n"
            "Добавить: <code>.bw add слово</code> · список: <code>.bw list</code> · "
            "выкл: <code>.bw off</code>"
        )
        return
    sub = args[0].lower()
    rest = ctx.arg.split(maxsplit=1)[1].strip() if len(args) > 1 else ""
    if sub == "add" and rest:
        add_ban_word(ctx.owner, rest)
        set_banwords(ctx.conn_id, True)
        await ctx.notify(f"➕ В бан-слова добавлено: <code>{safe(rest)}</code>")
    elif sub in ("del", "rm", "remove") and rest:
        del_ban_word(ctx.owner, rest)
        await ctx.notify(f"➖ Убрано из бан-слов: <code>{safe(rest)}</code>")
    elif sub == "list":
        words = list_ban_words(ctx.owner)
        body = ", ".join(safe(w) for w in words) if words else "—"
        await ctx.notify(f"🚫 <b>Бан-слова:</b>\n{body}")
    elif sub == "on":
        set_banwords(ctx.conn_id, True)
        await ctx.notify("🚫 Бан-слова включены.")
    elif sub == "off":
        set_banwords(ctx.conn_id, False)
        await ctx.notify("✅ Бан-слова выключены.")
    else:
        await ctx.notify("Использование: <code>.bw add|del|list|on|off</code>")


@command("unbw", help="Выключить бан-слова", category="Режимы")
async def cmd_unbw(ctx: Ctx) -> None:
    set_banwords(ctx.conn_id, False)
    await ctx.notify("✅ Бан-слова выключены.")


# ============================ Utility ============================

@command("afk", help="Автоответ пока тебя нет: .afk обедаю", category="Полезное")
async def cmd_afk(ctx: Ctx) -> None:
    reason = ctx.arg or "Сейчас не на месте, отвечу позже."
    afk[ctx.conn_id] = {"reason": reason, "seen": set()}
    await ctx.notify(f"🌙 AFK включён: <i>{reason}</i>\nСниму при <code>.unafk</code> или твоём сообщении.")


@command("unafk", help="Выключить автоответ", category="Полезное")
async def cmd_unafk(ctx: Ctx) -> None:
    afk.pop(ctx.conn_id, None)
    await ctx.notify("☀️ AFK выключен.")


# --- Mute: silently delete a contact's incoming messages ---------------------

_DUR_UNITS = {
    "s": 1, "sec": 1, "с": 1, "сек": 1,
    "m": 60, "min": 60, "м": 60, "мин": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "ч": 3600, "час": 3600,
    "d": 86400, "day": 86400, "д": 86400, "дн": 86400, "день": 86400,
}


def _parse_duration(arg: str) -> int | None:
    """'5min' / '30s' / '2h' / '1д' → seconds. Empty / junk → None (forever)."""
    arg = (arg or "").strip().lower()
    if not arg:
        return None
    m = re.fullmatch(r"(\d+)\s*([a-zа-яё]*)", arg)
    if not m:
        return None
    return int(m.group(1)) * _DUR_UNITS.get(m.group(2) or "min", 60)


def _human_left(secs: int) -> str:
    if secs >= 86400:
        return f"{secs // 86400} дн"
    if secs >= 3600:
        return f"{secs // 3600} ч"
    if secs >= 60:
        return f"{secs // 60} мин"
    return f"{secs} сек"


@command("mute", help="Глушить собеседника: .mute 30m (или бессрочно)", category="Полезное")
async def cmd_mute(ctx: Ctx) -> None:
    secs = _parse_duration(ctx.arg)
    until = int(time.time()) + secs if secs else None
    set_mute(ctx.conn_id, ctx.chat_id, until)
    when = f"на <b>{_human_left(secs)}</b>" if secs else "<b>бессрочно</b>"
    await ctx.notify(
        f"🔇 <b>Чат заглушён {when}.</b>\n"
        f"Новые сообщения собеседника я удаляю. <code>.unmute</code> — снять."
    )


@command("unmute", help="Снять мут с собеседника", category="Полезное")
async def cmd_unmute(ctx: Ctx) -> None:
    clear_mute(ctx.conn_id, ctx.chat_id)
    await ctx.notify("🔊 <b>Чат разглушён.</b> Сообщения снова приходят.")


def _parse_when(s: str) -> int | None:
    """'30m' / '2h' / '1d' → unix-ts; 'HH:MM' → next such time (in TZ_OFFSET)."""
    secs = _parse_duration(s)
    if secs:
        return int(time.time()) + secs
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        tz = timezone(timedelta(hours=TZ_OFFSET))
        now = datetime.now(tz)
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                             second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return int(target.timestamp())
    return None


@command("remind", "rem", help="Напоминание: .remind 30m текст / .remind 18:00 текст",
         category="Полезное")
async def cmd_remind(ctx: Ctx) -> None:
    parts = ctx.arg.split(maxsplit=1)
    if len(parts) < 2:
        await ctx.notify("⏰ Формат: <code>.remind 30m текст</code> или <code>.remind 18:00 текст</code>")
        return
    due = _parse_when(parts[0])
    if not due:
        await ctx.notify("⏰ Не понял время. Примеры: 30m, 2h, 1d, 18:00")
        return
    add_reminder(ctx.owner, ctx.conn_id, ctx.chat_id, due, parts[1])
    when = datetime.fromtimestamp(due, timezone(timedelta(hours=TZ_OFFSET))).strftime("%d.%m %H:%M")
    await ctx.notify(f"⏰ Напомню <b>{when}</b>:\n{safe(parts[1])}")


@command("table", "tbl", help="Таблица: .table, дальше строки, колонки через |",
         category="Полезное")
async def cmd_table(ctx: Ctx) -> None:
    table = _render_table(ctx.arg)
    if not table:
        await ctx.notify(
            "📊 <b>Как сделать таблицу</b>\n"
            "Пиши команду, дальше — строки с новой строки, колонки через <code>|</code>:\n\n"
            "<pre>.table\nПриз | 500$\nБюджет | 3000-5000$\nДедлайн | 10 дней</pre>\n"
            "Можно и в одну строку через <code>;</code>: "
            "<code>.table Приз | 500$ ; Дедлайн | 10д</code>"
        )
        return
    await ctx.send(table)


@command("id", help="Показать id чата и собеседника", category="Полезное")
async def cmd_id(ctx: Ctx) -> None:
    u = ctx.reply.from_user if ctx.reply else ctx.msg.chat
    name = getattr(u, "first_name", "") or ""
    uid = getattr(u, "id", ctx.chat_id)
    await ctx.notify(f"🆔 <b>{name}</b>\nchat: <code>{ctx.chat_id}</code>\nuser: <code>{uid}</code>")


@command("help", "cmds", "commands", help="Список команд", category="Полезное")
async def cmd_help(ctx: Ctx) -> None:
    by_cat: dict[str, list[Command]] = {}
    for cmd in _REGISTERED:
        by_cat.setdefault(cmd.category, []).append(cmd)
    lines = ["🛠 <b>Команды (пиши прямо в чате)</b>"]
    for cat, cmds in by_cat.items():
        lines.append(f"\n<b>{cat}</b>")
        for cmd in cmds:
            alias = " / ".join(f".{n}" for n in cmd.names)
            lines.append(f"<code>{alias}</code> — {cmd.help}")
    await ctx.notify("\n".join(lines))
