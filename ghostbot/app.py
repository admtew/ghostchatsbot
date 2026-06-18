"""Application wiring and runtime (webhook or polling)."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import (
    ALLOWED_UPDATES,
    BOT_TOKEN,
    HOST,
    PORT,
    WEBHOOK_PATH,
    WEBHOOK_URL,
    log,
)
from .handlers import router
from .storage import init_db


def build_bot() -> Bot:
    return Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def run() -> None:
    """Start the bot in webhook mode if WEBHOOK_URL is set, else long-polling."""
    init_db()
    log.info("Ghost Recovery Bot starting (%s mode)...",
             "webhook" if WEBHOOK_URL else "polling")

    from aiohttp import web

    bot = build_bot()
    dp = build_dispatcher()

    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", health)
    app.router.add_get("/", health)

    if WEBHOOK_URL:
        from aiogram.types import Update

        full_url = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        await bot.set_webhook(
            full_url,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=False,
        )
        log.info("webhook set: %s", full_url)

        async def handle_webhook(request: web.Request) -> web.Response:
            try:
                data = await request.json()
            except Exception as e:
                log.error("webhook: bad JSON: %s", e)
                return web.Response(text="ok")
            try:
                update = Update.model_validate(data, context={"bot": bot})
                await dp.feed_update(bot=bot, update=update)
            except Exception:
                # Always 200 so Telegram doesn't hammer us with retries; the
                # full traceback is logged for debugging.
                log.exception("webhook: failed to process update keys=%s",
                              list(data.keys()))
            return web.Response(text="ok")

        app.router.add_post(WEBHOOK_PATH, handle_webhook)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PORT)
        await site.start()
        log.info("listening on %s:%s", HOST, PORT)
        await asyncio.Event().wait()
    else:
        # Health server alongside polling (Railway/Procfile health checks).
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PORT)
        await site.start()
        log.info("health server on %s:%s, starting polling...", HOST, PORT)

        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
