"""Ghost Recovery Bot — entry point.

Recovers messages your contacts delete, edit, or send as self-destruct media in
your Telegram Business chats, and forwards copies to you only.

The implementation lives in the ``ghostbot`` package; this file just starts it,
so the deploy command (``python main.py``) never has to change.
"""

from __future__ import annotations

import asyncio

from ghostbot.app import run
from ghostbot.config import log

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped.")
