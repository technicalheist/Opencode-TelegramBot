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
  The selected model is stored **per (user, server)**, so switching servers never
  carries a model over; an unset model means the server default.
- **Directory browser** — `/workdir` lets you browse the **opencode server's**
  filesystem with buttons and pick the working directory for the agent. The
  workdir is stored **per (user, server)**, so switching servers never carries a
  path over; a path that is not browsable on the current server is reset to that
  server's default.
- **Task status** — `/status` shows the running prompt, elapsed time, the latest
  activity, and the agent's todo list.
- **Multiple opencode servers** — register extra servers with `/server` and
  switch between them per user; the configured `OPENCODE_BASE_URL` is the default.
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
| `OPENCODE_BASE_URL` | `http://localhost:4096` | Default opencode server URL (registered as `Local`; host/port are used to start it) |
| `OPENCODE_DIRECTORY` | repo root | Default working directory (fallback when the server's `GET /path` is unavailable) |
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

## Running it

Manage the launcher as a background process with the helper scripts:

```powershell
scripts\bot.ps1 start     # start in the background (logs to logs\bot.out.log / logs\bot.err.log)
scripts\bot.ps1 status    # launcher pid, opencode port, health
scripts\bot.ps1 stop      # stop the bot and the opencode server it manages (never cloudflared)
```

```bash
./scripts/bot.sh start
./scripts/bot.sh status
./scripts/bot.sh stop
```

The pid file lives in `run/bot.pid` and logs in `logs/` (both gitignored).
`stop` also terminates the process listening on the configured opencode port;
it never touches cloudflared — the launcher's own `finally` stops the tunnel it
started. `status` exits non-zero when nothing is running.

### Cross-platform

Linux/macOS work too (no Windows-only assumptions in the Python code; the
scripts have POSIX counterparts). Prerequisites: Python 3.14+ with a
`.venv` (`.venv/bin/python`), `opencode` on `PATH`, and — for webhook mode —
`cloudflared` configured via `CLOUDFLARED_COMMAND` (an absolute path is fine).
`scripts/bot.sh` uses `lsof` for port lookup when available and falls back to
`psutil`; `curl` is used for health when present, otherwise the venv Python.

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
| `/workdir` | Browse and set the working directory with **buttons** (a path on the selected opencode server) |
| `/compact` | Compact the current session (fire-and-forget) |
| `/mcp` | List MCP servers, status, and tool ids |
| `/server` | List/switch opencode servers (buttons); `add <url> [label]`, `use <id>`, `remove <id>` (admin only), `list` |
| `/list` | List stored media as tappable **buttons** |
| `/get <id>` | Send a stored media item by id |

> opencode features require an authenticated user. New users start
> unauthenticated; the admin is seeded from `ADMIN_USER_ID`.

## User approval

New users start unauthenticated. The first time an unknown user messages the bot,
the bot forwards an access request to `ADMIN_USER_ID` (name, username, Telegram
id, language) with **✅ Approve** / **🚫 Reject** inline buttons and replies to the
user that their access is pending. While unauthenticated, the only commands that
reach their handlers are `/start` and `/id`; everything else gets the
access-pending message. Tapping **Approve** sets `is_authenticated = 1` and DMs
the user; **Reject** keeps them unauthenticated and DMs them that the request was
declined. The request is sent once per pending state (rejecting clears it, so a
later message can re-notify the admin).

## Custom opencode servers

The bot talks to `OPENCODE_BASE_URL` (default `http://localhost:4096`), which is
registered as the default `Local` server at startup. Authenticated users can add
more servers and switch between them; the choice is per user.

- `/server` lists every registered server as an inline button (the active one is
  marked `✅`) plus a `➕ Add server` button. Admins also get remove buttons.
- `/server add <url> [label]` (or the `➕ Add server` button) starts an
  interactive prompt: URL → username → password. Send `/skip` to leave a field
  empty and `/cancel` to abort. After the health check the server is added and
  selected. `/server use <id>` switches to an existing server.
- **Credentials** are optional. A username selects HTTP **Basic** auth (empty
  password allowed); a secret with no username selects **Bearer** auth. They are
  stored in plaintext in the local (gitignored) SQLite database, never logged or
  echoed, and shown only as `🔒 <username>` / `🔒 token`. The password message is
  deleted from the chat afterwards. Only add servers you trust.
- `/server remove <id>` (admin only) detaches users on it and drops its sessions;
  the last remaining server cannot be removed.
- Switching to a different server **deletes your current session**, so the next
  prompt starts a fresh session on the new server.

A server running in Docker reaches the host opencode server via
`http://host.docker.internal:4096` (or a LAN URL); a plain local server is
`http://localhost:4096`. Only add servers you trust — the agent can read and
write files in each server's working directory.

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
| Replies never arrive | Ensure `OPENCODE_BASE_URL` points at your running server and its default directory (`GET /path`) is valid. |
| Agent blocks on a permission prompt | Tap the Allow/Reject buttons, or set broader defaults in your opencode config. |
| Voice input fails | Set `STT_API_KEY` and a valid `STT_MODEL`. |
| Workdir looks wrong | Run `/workdir` and pick a directory **on the opencode server** (the browser lists the server's filesystem, not the bot host's); the workdir is stored per (user, server), and a stale/invalid stored workdir is reset to that server's default. |

## Security notes

- `.env`, `data/`, and `media/` are gitignored. **Never commit them.**
- Rotate `TELEGRAM_BOT_TOKEN` / `STT_API_KEY` if they are ever exposed.
- opencode features are restricted to authenticated users. New users start
  unauthenticated and must be approved by the admin (see **User approval**).
- The bot can read/write files in the configured working directory; run it only
  on machines and directories you trust.

## License

MIT — see [LICENSE](LICENSE).
