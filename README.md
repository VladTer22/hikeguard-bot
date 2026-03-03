# HikeGuard

Anti-spam Telegram bot for group chats.

## Detection layers

Bot uses a multi-layer approach — each new member goes through all checks in order:

**Layer 0 — CAS (Combot Anti-Spam)**
When a user joins, the bot checks them against the [CAS](https://cas.chat/) global spam database. Known spammers are banned immediately.

**Layer 1 — Quarantine**
New members can only send text messages for the first 24 hours (configurable). Photos, videos, documents, stickers, and voice messages are blocked. Permissions are restored automatically by Telegram after the quarantine period.

**Layer 2 — Keyword scoring**
Every text message and media caption is scored against a dictionary of spam indicators. Each keyword has a weight (score) reflecting how strongly it signals spam:

- **1–2 points** — words that appear in normal conversations too (salary, bonus, payment)
- **3–4 points** — typical spam phrases (vacancy, sales manager, no experience needed)
- **5 points** — almost certainly spam (work abroad, you have won)

The bot sums up all matched keywords in a message. If the total reaches the threshold (default: 7), the message is deleted as spam. A single low-weight word won't trigger detection — only a combination of spam indicators will.

Example — spam detected:
> "Vacancy, sales manager, salary from 1000$" → "vacancy" (4) + "sales manager" (4) = 8 >= 7 → spam

Example — not spam:
> "What's the salary for guides?" → "salary" (2) = 2 < 7 → ok

Additional scoring sources:
- ~60 built-in keywords and phrases (Ukrainian, Russian, English) using stem-based matching to handle declensions
- Regex patterns for phone numbers, money amounts, work schedules, Telegram usernames, URLs, and known scam domains
- Custom keywords added by admins at runtime via `/add_word`
- Longer phrases are matched first to avoid double-counting

**Layer 3 — Gemini Vision**
Photos and GIF thumbnails that pass keyword scoring are sent to Google Gemini for visual spam classification. Results are cached by `file_unique_id` to avoid duplicate API calls. If Gemini confidence is below 70%, the message is forwarded to admins for manual review instead of auto-deletion.

Gemini is optional — the bot works in keywords-only mode without an API key.

## Moderation actions

- First spam strike — message deleted, user muted for 60 minutes
- Second strike — permanent ban with message history revocation (configurable per-user via `/set_limit`)
- Every action is logged to the database and reported to the admin chat with details (score, method, matched keywords, Gemini reason)
- A random Ukrainian-language reply is posted in the chat when spam is deleted (auto-deleted after 30 seconds)

## Admin commands

| Command | Description |
|---|---|
| `/spam` | Mark as spam: instant for admins, community vote (5 votes) for regular users |
| `/trust` | Mark user as trusted: removes quarantine, resets strikes, photos skip Gemini (reply to their message) |
| `/untrust` | Revoke trusted status — user goes back to full spam checks (reply to their message) |
| `/mute [minutes]` | Mute a user for N minutes, default 60 (reply to their message) |
| `/unmute` | Remove mute from a user without changing anything else (reply to their message) |
| `/set_limit <N>` | Set per-user mute limit before ban (reply to their message). `/set_limit reset` restores default |
| `/status` | Spam statistics: 24h, 7 days, all time, top detection methods |
| `/spam_words` | List all custom keywords with scores |
| `/add_word <word> [score]` | Add a custom keyword (default score: 3) |
| `/remove_word <word>` | Remove a custom keyword |
| `/chatid` | Show current chat ID and your user ID |

All admin commands work only for chat administrators (`/spam` also works for regular users via voting). Command messages and bot replies are auto-deleted to keep the chat clean.

## Setup

```bash
cp .env.example .env
# fill in BOT_TOKEN and ADMIN_CHAT_ID (required), GEMINI_API_KEY (optional)
```

### Docker (recommended)

```bash
docker compose up -d
```

### Local

```bash
poetry install
python bot.py
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | — | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_CHAT_ID` | — | Chat ID where spam reports are sent |
| `QUARANTINE_HOURS` | `24` | Media restriction period for new members |
| `SPAM_THRESHOLD` | `7` | Minimum keyword score to classify as spam |
| `GEMINI_API_KEY` | — | Google Gemini API key (optional) |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Gemini model for image classification |
| `GEMINI_TIMEOUT` | `10` | Gemini API timeout in seconds |
| `MUTE_DURATION_MINUTES` | `60` | Mute duration on first spam strike |
| `BAN_ON_STRIKE` | `2` | Ban on Nth spam strike (1 = instant ban, 2 = one mute then ban) |
| `DB_PATH` | `data/hikeguard.db` | SQLite database path |

## Tech stack

- Python 3.12+, [aiogram 3](https://github.com/aiogram/aiogram)
- Google Gemini API via [google-genai](https://github.com/googleapis/python-genai)
- SQLite (aiosqlite, WAL mode)
- Docker, Poetry
