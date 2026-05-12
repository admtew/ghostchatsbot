# Ghost Recovery Bot

Anonymous, selfâhosted Telegram bot that brings back messages your contacts delete from your Business chats â text, photo, video, voice, videoânote, sticker, GIF, audio, document â and rescues selfâdestruct media on demand.

Works like *dialogspybot*, but everything stays on **your** server. No thirdâparty cloud, no history sharing, no analytics.

---

## Features

- Catches deleted messages via Telegram Business API and forwards them to you only
- Rescues selfâdestruct photos/videos: reply to the message with anything and the bot resends the cached copy
- Stores messages locally in a single SQLite file (WAL mode, fast)
- Multiâuser safe: every connection is keyed to its owner, no crossâuser leaks
- `/wipe` command to erase your cache at any moment
- One file, ~300 lines, ready to fork

## Requirements

- Python 3.11+
- A Telegram **Premium** account (Business features require it)
- A bot token from [@BotFather](https://t.me/BotFather)
  - In BotFather: `/mybots â Bot Settings â Business Mode â Enable`

## Install

```bash
git clone <your-fork-url> ghost-recovery-bot
cd ghost-recovery-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# put your BOT_TOKEN inside .env
python main.py
```

## Connect the bot to your account

1. Telegram â **Settings** â **Telegram Business** â **Chatbots**
2. Type your bot's username and tap **Add**
3. Allow it to read and manage messages
4. The bot DMs you: `â ÐÐ¾Ñ Ð¿Ð¾Ð´ÐºÐ»ÑÑÑÐ½`

That's it. From now on, every deleted message in any of your chats will arrive in your DM with the bot.

## Selfâdestruct media

If someone sends a oneâview photo/video, **either**:

- reply to it with any character (`+`, `.`, anything), or
- put any reaction (â¤ï¸, ð, anything) on it.

The bot will resend the cached copy to your DM.

## Commands

| Command | What it does |
|---|---|
| `/start` | Help screen |
| `/status` | Connection status + how many messages cached |
| `/wipe` | Delete everything the bot stored for you |

## Security & privacy

- Token lives in `.env` (gitignored)
- Database is local; only the connection owner can read their messages
- `business_connection_id â owner_id` mapping enforces isolation
- No webhooks, no external services, only Telegram's API
- For production: run behind systemd or Docker, restrict file permissions on `data.db`

## Deploy with systemd (optional)

```ini
# /etc/systemd/system/ghostbot.service
[Unit]
Description=Ghost Recovery Bot
After=network.target

[Service]
WorkingDirectory=/opt/ghostbot
ExecStart=/opt/ghostbot/.venv/bin/python main.py
Restart=always
User=ghostbot

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ghostbot
```

## License

MIT â do whatever you want, just don't blame the author.
