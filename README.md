# Opencode-TelegramBot

A Telegram bridge for [opencode](https://opencode.ai) with voice and media support.
Chat with your local opencode server from Telegram: send text or voice notes, get
text or spoken replies, share images/files with the agent, receive files it
creates, and approve tool-permission requests with inline buttons.

> Status: works and is used daily. Requires a running local opencode server.

## Features

- **Text chat** — a Telegram message becomes an opencode prompt; the assistant
  reply is sent back (long replies are split at Telegram's 4096-char limit).
- **Voice mode** — toggle per user with `/voice`. When ON, replies are also sent
  as a voice note (Edge TTS) alongside the text; when OFF, TTS is never used.
- **Speech-to-text** — voice notes/audio are transcribed via OpenRouter's
  `/audio/transcriptions` endpoint and sent to opencode as text.
- **Inbound media** — photos, videos, documents, animations, video notes, and
  stickers are stored locally and forwarded to opencode as a prompt that
  references the file's absolute path, so the agent's `read` tool can open them.
- **Outbound media** — files returned by the agent (`file` parts) and media files
  it creates/modifies during a task are sent back to your Telegram chat
  (images as photos, everything else as documents), capped at `MEDIA_MAX_MB`.
- **Interactive permissions** — when opencode asks to run a tool, you get
  Allow once / Always / Reject buttons; your choice resumes the agent.
- **Model & session pickers** — `/models` and `/session` are inline-button menus.
- **Directory browser** — `/workdir` lets you browse the filesystem with buttons
  and pick the working directory for the agent.
- **Task status** — `/status` shows the running prompt, elapsed time, the latest
  activity, and the agent's todo list.
- **Two-way media store** — every incoming media file is kept under `media/` and
  recorded in SQLite; `/list` and `/get` retrieve them.

## Architecture

```
opencode-voice/            (published as Opencode-TelegramBot)
├── telegram_bot/          Telegram interface: handlers, SQLite, media store, SSE
├── opencode_client/       Async HTTP + SSE client for the local opencode server
├── stt/                   Speech-to-text via OpenRouter
├── tts/                   Text-to-speech via Edge TTS
├── config.py              Shared env parsing and paths
├── requirements.txt
└── tests/                 pytest suite (no network calls)
```

Everything is async (`python-telegram-bot` v22). One persistent opencode session
is kept per Telegram user (`/new` resets it). A background SSE listener streams
opencode events to surface permission requests.

## Prerequisites

- **Python 3.14+**
- The **opencode** CLI on your `PATH` (the single launcher starts `opencode serve`
  for you; you can also run your own server)
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)
- An **OpenRouter API key** for STT (only needed for voice input)
- Your Telegram **numeric user id** (get it from [@userinfobot](https://t.me/userinfobot))

## Setup

```powershell
git clone https://github.com/technicalheist/Opencode-TelegramBot.git
cd Opencode-TelegramBot

python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

copy .env.example .env
# edit .env and fill in the values below
```

On macOS/Linux use `python3 -m venv .venv` and `.venv/bin/python`.

### Configuration (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather (**required**) |
| `ADMIN_USER_ID` | — | Your Telegram numeric id; seeded as the admin (**required**) |
| `DB_PATH` | `data/bot.db` | SQLite database path |
| `MEDIA_DIR` | `media` | Where downloaded media is stored |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `STT_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter base URL |
| `STT_API_KEY` | — | OpenRouter API key (for voice input) |
| `STT_MODEL` | `openai/whisper-large-v3` | Transcription model slug |
| `STT_PROVIDER` | `api` | Reserved |
| `TTS_VOICE` | `en-US-AriaNeural` | Edge TTS voice |
| `TTS_RATE` / `TTS_VOLUME` / `TTS_PITCH` | `+0%` / `+0%` / `+0Hz` | Edge TTS prosody |
| `OPENCODE_BASE_URL` | `http://localhost:4096` | opencode server URL (host/port are used to start it) |
| `OPENCODE_DIRECTORY` | repo root | Default working directory |
| `OPENCODE_AGENT` | `build` | opencode agent to use |
| `OPENCODE_TIMEOUT` | `600` | Seconds to wait for a blocking prompt |
| `OPENCODE_SERVE_COMMAND` | `opencode` | Command used to launch `opencode serve` |
| `MEDIA_MAX_MB` | `20` | Max size for bridged media |
| `TELEGRAM_WEBHOOK_URL` | — | Public HTTPS base URL; **empty = long polling** |
| `TELEGRAM_WEBHOOK_PORT` | `8080` | Local port the webhook server listens on |
| `TELEGRAM_WEBHOOK_PATH` | — | Secret path segment Telegram posts to |
| `TELEGRAM_WEBHOOK_SECRET` | — | `X-Telegram-Bot-Api-Secret-Token` value |
| `CLOUDFLARED_COMMAND` | `cloudflared` | Path to the `cloudflared` binary |
| `CLOUDFLARED_TUNNEL_TOKEN` | — | Remote-managed tunnel token (launcher runs it) |

## Run

One command starts everything. `python -m telegram_bot`:

1. installs the global MCP tool + skill (`~/.config/opencode/`) idempotently,
2. stops any stale `opencode serve` on the configured port,
3. starts `opencode serve --port … --hostname …`,
4. waits until the server is healthy, then
5. runs the Telegram bot (and stops the server it started on exit).

```powershell
.venv\Scripts\python -m telegram_bot
```

Because the MCP config and skill are installed before the server starts, the
freshly started opencode picks them up. The bot logs `Bot started` and begins
long polling; open your bot in Telegram and send `/start`.

If you prefer to manage your own opencode server, start it yourself and run the
bot directly:

```powershell
.venv\Scripts\python -m telegram_bot.bot
```

In that mode, restart opencode after the bot has installed the MCP config/skill
so it loads them.

## Webhook mode (cloudflared)

By default the bot uses long polling (works offline, no public URL). Setting
`TELEGRAM_WEBHOOK_URL` switches `python -m telegram_bot` to webhook mode:

- the bot listens on `127.0.0.1:TELEGRAM_WEBHOOK_PORT` at
  `/<TELEGRAM_WEBHOOK_PATH>` and registers
  `https://<host>/<TELEGRAM_WEBHOOK_PATH>` with Telegram, using
  `TELEGRAM_WEBHOOK_SECRET` as the request secret;
- the launcher starts `cloudflared tunnel --no-autoupdate run --token …` and
  waits for the public URL to respond (it continues with a warning if the
  tunnel is not up yet), and stops cloudflared on exit alongside `opencode
  serve`.

The Cloudflare side here is **remotely-managed**: a tunnel named
`telegram-bot` with ingress `telegram-bot.shivrajan.com → http://localhost:8080`
and a proxied CNAME to `<tunnel-id>.cfargotunnel.com`. The token is the only
secret the launcher needs; set the keys above in `.env`.

To revert to long polling, empty `TELEGRAM_WEBHOOK_URL` (the launcher then skips
cloudflared and the bot polls).

## Commands

| Command | Description |
| --- | --- |
| `/start` | Greeting and auth state |
| `/help` | Command list |
| `/id` | Your Telegram id |
| `/whoami` | Your profile, role, voice mode, workdir, model |
| `/new` | Start a fresh opencode session |
| `/stop` | Abort the running opencode prompt |
| `/status` | Show the current task status (activity + todos) |
| `/voice` | Voice mode with ON/OFF **buttons** |
| `/voice on` \| `/voice off` | Enable/disable spoken replies |
| `/models` | Pick the model with **buttons** |
| `/session` | Pick an existing session with **buttons** |
| `/workdir` | Browse and set the working directory with **buttons** |
| `/compact` | Compact the current session (fire-and-forget) |
| `/mcp` | List MCP servers, status, and tool ids |
| `/list` | List stored media as tappable **buttons** |
| `/get <id>` | Send a stored media item by id |

> opencode features require an authenticated user. New users start
> unauthenticated; the admin is seeded from `ADMIN_USER_ID`.

## How it works

**Text flow** — message → opencode `POST /session/{id}/message` → assistant text
→ Telegram (plus a voice note when voice mode is ON).

**Voice flow** — voice note → downloaded and stored → STT → opencode → reply.
With voice mode OFF the reply is text only and TTS is never called.

**Inbound media** — stored under `media/<telegram_id>/`, recorded in SQLite, then
forwarded to opencode as:

```
The user sent a photo.
See the photo from this path: D:\...\media\<id>\<file>.jpg
Caption: <your caption>
```

**Outbound media** — after a prompt, `file` parts and media files created or
modified by the task are downloaded and sent to you. Paths are confined to the
session directory and deduplicated per session.

**Permissions** — the SSE listener turns `permission.asked` events into inline
buttons. Tapping one replies to opencode and the agent continues.

## Tests

```powershell
.venv\Scripts\python -m pytest tests
```

The suite is fully offline (HTTP is mocked with `httpx.MockTransport`, TTS/Send
use fakes) and does not require a real bot token.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Sorry, Opencode could not complete that request.` | Check the opencode server is running and the selected model is valid (`/models`); see the bot log for the HTTP status. |
| Replies never arrive | Ensure `OPENCODE_BASE_URL` points at your running server and `OPENCODE_DIRECTORY` is a real directory. |
| Agent blocks on a permission prompt | Tap the Allow/Reject buttons, or set broader defaults in your opencode config. |
| Voice input fails | Set `STT_API_KEY` and a valid `STT_MODEL`. |
| Workdir looks wrong | Run `/workdir` and pick a directory; use an absolute path with a drive letter on Windows. |

## Security notes

- `.env`, `data/`, and `media/` are gitignored. **Never commit them.**
- Rotate `TELEGRAM_BOT_TOKEN` / `STT_API_KEY` if they are ever exposed.
- opencode features are restricted to authenticated users (the admin by default).
  Pair this with Phase 2 approval if you expose the bot to others.
- The bot can read/write files in the configured working directory; run it only
  on machines and directories you trust.

## License

MIT — see [LICENSE](LICENSE).
