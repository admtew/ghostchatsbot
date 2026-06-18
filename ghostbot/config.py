"""Configuration and constants.

Everything that depends on the environment lives here, so the rest of the
package never touches ``os.getenv`` directly. Edit values here or via ``.env``.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")

# Support contact shown in /start and /help.
SUPPORT_CONTACT: str = os.getenv("SUPPORT_CONTACT", "@qstaeg")

# --- Storage ----------------------------------------------------------------
_default_db = "/data/ghost.db" if os.path.isdir("/data") else "data.db"
DB_PATH: str = os.getenv("DB_PATH", _default_db)

# How many cached messages to keep in the hot in-memory layer. This is the
# main defence against the "delete arrives before save" race; bump it if you
# have very busy chats.
MEMORY_CACHE_SIZE: int = int(os.getenv("MEMORY_CACHE_SIZE", "5000"))

# --- Webhook / server -------------------------------------------------------
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")  # if set → webhook mode
WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8080"))

# --- Behaviour --------------------------------------------------------------
# When a delete arrives but the message is not yet stored, wait and retry —
# the corresponding business_message update may still be in flight.
RECALL_RETRIES: int = int(os.getenv("RECALL_RETRIES", "6"))
RECALL_RETRY_DELAY: float = float(os.getenv("RECALL_RETRY_DELAY", "0.4"))  # seconds

# How many resends to flush at once when a whole batch is deleted.
BATCH_DISPLAY_LIMIT: int = int(os.getenv("BATCH_DISPLAY_LIMIT", "30"))

# Retries for outgoing sends that hit Telegram flood limits (429).
SEND_RETRIES: int = int(os.getenv("SEND_RETRIES", "4"))

# --- Updates we ask Telegram for -------------------------------------------
ALLOWED_UPDATES = [
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]

# --- Logging ----------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ghostbot")
