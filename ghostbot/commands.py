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

import ast
import asyncio
import operator
import random
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.types import Message

from .actions import afk, delete_msgs
from .config import log
from .storage import clear_mute, set_mute

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
        return await self.bot.send_message(
            self.chat_id, text, business_connection_id=self.conn_id, **kw
        )

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
    head, _, rest = text[1:].partition(" ")
    cmd = COMMANDS.get(head.lower())
    if not cmd:
        return False

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
    "Бесспорно", "Можешь не сомневаться", "Да", "Скорее всего",
    "Хорошие перспективы", "Знаки говорят — да", "Пока не ясно, попробуй ещё",
    "Спроси позже", "Не сейчас", "Даже не думай", "Мой ответ — нет",
    "Очень сомнительно", "Никаких шансов",
]


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


# ============================ Safe calculator ============================

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _calc(expr: str) -> float:
    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError("недопустимое выражение")

    return ev(ast.parse(expr, mode="eval").body)


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


@command("choose", "pick", help="Выбрать вариант: .choose чай|кофе", category="Развлечения")
async def cmd_choose(ctx: Ctx) -> None:
    sep = "|" if "|" in ctx.arg else ","
    options = [o.strip() for o in ctx.arg.split(sep) if o.strip()]
    if options:
        await ctx.send(f"👉 {random.choice(options)}")


# ============================ Utility ============================

@command("del", "d", help="Удалить сообщение (ответом на него)", category="Полезное")
async def cmd_del(ctx: Ctx) -> None:
    if ctx.reply:
        await ctx.delete(ctx.reply.message_id)


@command("edit", "e", help="Изменить своё сообщение (ответом): .edit новый текст", category="Полезное")
async def cmd_edit(ctx: Ctx) -> None:
    if ctx.reply and ctx.arg:
        try:
            await ctx.edit(ctx.reply.message_id, ctx.arg)
        except Exception as e:
            await ctx.notify(f"⚠️ Не вышло изменить: {e}")


@command("calc", "c", help="Калькулятор: .calc 2+2*3", category="Полезное")
async def cmd_calc(ctx: Ctx) -> None:
    if not ctx.arg:
        return
    try:
        result = _calc(ctx.arg)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        await ctx.send(f"{ctx.arg} = {result}")
    except Exception:
        await ctx.notify("⚠️ Не смог посчитать выражение.")


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
