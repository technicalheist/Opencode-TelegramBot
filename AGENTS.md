# AGENTS.md

## Project Overview

`opencode-voice` is a multi-module Python project that bridges opencode with
Telegram. It is laid out as a root-level project: shared configuration lives in
`config.py` at the repo root, and each capability is a top-level module.

Modules:

- `telegram_bot/` — Telegram interface: two-way media sync, command handlers,
  and the opencode bridge.
- `stt/` — speech-to-text via OpenRouter's `/audio/transcriptions` endpoint.
- `tts/` — text-to-speech via Microsoft Edge TTS (`edge-tts`).
- `opencode_client/` — async HTTP client for the local opencode server API.

Phase 1 (Telegram two-way media sync) and Phase 1.5 (root STT/TTS modules) are
complete and verified. **Phase 3 (current)** wires Telegram ↔ opencode with text
and voice round trips and interactive permission handling.

## Phase Plan

### Phase 1 - Telegram bot with two-way media sync (DONE)
- [x] Scaffold `telegram_bot/` package with dependency + env setup.
- [x] SQLite persistence layer with a `users` table (`is_authenticated` flag,
      `role`, Telegram profile fields, timestamps).
- [x] Seed the admin user on first run.
- [x] Bot entrypoint using `python-telegram-bot` v22 (async) with long polling.
- [x] Inbound media (photo, video, audio, voice, document, animation, video
      note, sticker) saved under `media/` and recorded in SQLite.
- [x] Outbound media send with `/list` + `/get <id>`.
- [x] Commands: `/start`, `/help`, `/id`, `/whoami`, `/list`, `/get <id>`.
- [x] Graceful shutdown, logging, error handling, `/get` owner/admin gate.

### Phase 1.5 - Root modules: STT + TTS (DONE)
- [x] Project-level files (`.env`, `.env.example`, `requirements.txt`,
      `config.py`, `.venv`) moved to the repo root.
- [x] Root `stt/` module (OpenRouter) and `tts/` module (Edge TTS).
- [x] Tests for `stt` and `tts` that do NOT hit the network.
- [x] Live-verified: voice note → `stt.transcribe` → text; text → `tts.synthesize`
      → mp3.

### Phase 2 - Admin-driven user authentication (PENDING)
- [ ] New users start `is_authenticated = 0`.
- [ ] Admin receives an authentication request and approves/rejects via inline
      buttons; decision is persisted in SQLite.
- [ ] Unauthenticated users are limited to `/start` and `/id`.

### Phase 3 - opencode integration + voice mode (DONE)
- [x] Root `opencode_client/` async client over the local opencode HTTP API.
- [x] One persistent opencode session per Telegram user, stored in SQLite;
      `/new` starts a fresh session.
- [x] Text flow: user text → opencode prompt → assistant text → Telegram reply.
- [x] Voice flow: voice/audio → `stt` → opencode → reply. With voice mode ON the
      reply is sent as voice (+ text); with it OFF, text only and TTS is never
      called.
- [x] `/voice [on|off]` per-user feature flag persisted in SQLite.
- [x] opencode permission requests surfaced to Telegram as inline buttons
      (Allow once / Always / Reject); the choice resumes the agent.
- [x] Access control: opencode features require `is_authenticated = 1`
      (admin until Phase 2 lands).
- [x] Tests: opencode client mocked with `httpx.MockTransport`; handler/DB tests
      make no network calls.
- [x] Update `telegram_bot/README.md`.

### Phase 3.5 - opencode command palette (DONE)
- [x] `/models` lists connected-provider models as inline buttons and persists
      the selection (`users.model_provider` / `users.model_id`).
- [x] `/session` lists sessions whose directory matches the user's workdir and
      lets the user switch the active session.
- [x] `/workdir [path]` shows/sets the workdir (validated directory) and starts
      a fresh session there.
- [x] `/compact` compacts the active session.
- [x] `/mcp` lists MCP servers, connection status, and tool ids.
- [x] `/help`, `/whoami`, and the Telegram command menu updated.
- [x] Tests for the new client methods and the model/session callback helpers.

### Phase 3.6 - interactive workdir browser (DONE)
- [x] `/workdir <path>` opens an inline directory browser at `<path>` that lists
      child directories as buttons (paginated) plus a preview of files.
- [x] Tapping a child navigates into it; `⬆️ Parent` and `🔄 Refresh` navigate;
      `✅ Use this directory` sets the workdir and starts a new session.
- [x] `/workdir` with no argument opens the browser at the current workdir.
- [x] Callback data stays within Telegram's 64-byte limit (index-based tokens).
- [x] Only authenticated users may browse; `OSError`/permission errors are shown,
      not raised.
- [x] Tests for the browser helpers and callbacks.

### Phase 3.7 - task status command (DONE)
- [x] `opencode_client.get_session_status()` (`GET /session/status`) and
      `get_session_todos(session_id)` (`GET /session/{id}/todo`), plus
      `get_last_activity(session_id)` from the latest assistant message parts.
- [x] The bot tracks in-flight prompts per user (`ACTIVE_TASKS`: session id,
      prompt, start time), set/cleared around the blocking prompt call.
- [x] **`Application` must run with `concurrent_updates` enabled** so `/status`,
      permission buttons, and other commands are processed while a prompt awaits
      (PTB's default is sequential and would block them).
- [x] `/status` reports the ongoing task: status, elapsed time, latest activity
      (`reasoning`/`text`/`tool:<name>`), and the session todo list with progress
      icons. When nothing is running it replies "No ongoing task found."
- [x] Note: this server's `/session/status` returns `{}` for v1 sessions even
      while busy, so the local `ACTIVE_TASKS` tracking is authoritative.
- [x] Only authenticated users; errors are shown, not raised.
- [x] `/help` and the Telegram command menu include `/status`.
- [x] Tests for the client methods, the concurrency setting, and the status
      handler (no network).

### Phase 3.8 - option buttons (DONE)
- [x] `/voice` shows the current mode and ON/OFF inline buttons; tapping one
      persists the flag and updates the message (callbacks `voice:on`/`voice:off`).
      `/voice on|off` continues to work.
- [x] `/list` renders each stored media item as an inline button
      (`media:<id>`) that sends that file; `/get <id>` continues to work.
- [x] Media buttons respect the existing owner/admin gate.
- [x] Audit: every command that takes an option/parameter is button-driven
      (`/workdir` browser, `/models`, `/session`, `/voice`, `/list`).
- [x] Tests for the voice/media callbacks and keyboards (no network).

### Phase 4 - media bridge (DONE)
Inbound: the bot routes text and voice/audio (via STT) to opencode; other media
are stored but invisible to opencode. Outbound: the bot only reads `text` parts.

**Phase 4.1 - inbound media references (DONE)**
- [x] For authenticated users, incoming photos/videos/documents/animations/
      video-notes/stickers are stored as now AND forwarded to opencode as a
      text prompt that references the absolute local path:
      `The user sent a <type>.\nSee the <type> from this path: <absolute path>\nCaption: <caption>`.
      opencode's `read` tool can open it.
- [x] Voice/audio keep the STT flow; unauthenticated users keep the
      storage-only reply.
- [x] The forward uses the existing session/lock/delivery so voice mode and
      `/status` tracking still apply.
- [x] Tests (no network).

**Phase 4.2 - outbound media (DONE)**
- [x] After a prompt, `file` parts in the assistant message are downloaded and
      sent to the user (images as photos, everything else as documents), capped
      at `MEDIA_MAX_MB` (default 20).
- [x] Media files created/modified by the task (assistant `patch` parts and/or
      `GET /session/{id}/diff`) are sent once, filtered by media extension,
      existence, size, and deduplicated per session.
- [x] `url` values may be `data:` URLs, `file:` URLs, or http(s) (including the
      local opencode server); handle all, and never send outside the session.
- [x] Tests (no network; fake downloads/senders).

### Phase 5 - question prompts (DONE)
opencode's `question` tool blocks the agent until answered. The bot must surface
`question.asked` events in Telegram (like permissions) — the SSE listener
currently ignores them.

- [x] `opencode_client.list_questions(*, directory=None)` (`GET /question`),
      `reject_question(request_id)` (`POST /question/{id}/reject`); `reply_question`
      already exists (`{"answers": [[labels], ...]}`).
- [x] On `question.asked`, map `sessionID` → user and send the question(s) with
      inline buttons: one button per option; `✅ Done` for multi-select; `✏️ Type
      answer` when `custom`; `🚫 Skip` to reject.
- [x] Collect answers for all questions in order, then call `reply_question`;
      clear the pending state on `question.replied`/`question.rejected`.
- [x] Callback data stays ≤64 bytes (per-user state + indices, no request id).
- [x] `text_message` treats the next message as the answer when a question is
      awaiting free-text input.
- [x] On startup, reconcile pending questions (`GET /question`) so a question
      that arrived before a restart is still surfaced.
- [x] Only authenticated users; errors shown, not raised.
- [x] Tests (no network).

### Phase 6 - explicit Telegram send (MCP tool + global skill) (CURRENT)
The pull-based patch/file method stays, but it is inference-based. Add an
explicit, reliable path: a local MCP tool the agent can call, plus a global
skill that documents the fallback CLI. Both are installed globally when the bot
starts (skip if already present).

- [ ] `telegram_bot/send_media.py` — CLI: `python -m telegram_bot.send_media
      <path> [--caption TEXT] [--chat-id ID]` sends a file via the Bot API.
- [ ] `telegram_bot/mcp_server.py` — local stdio MCP server exposing a
      `telegram_send_file(path, caption?)` tool (default chat = `ADMIN_USER_ID`).
- [ ] `telegram_bot/opencode_setup.py` — on startup, best-effort and idempotent:
      merge `mcp.telegram` into `~/.config/opencode/opencode.json` and write
      `~/.config/opencode/skills/telegram-media/SKILL.md`; never corrupt an
      existing config (skip on invalid JSON).
- [ ] Add `mcp` to `requirements.txt`.
- [ ] Wire the installer into `bot.post_init`; errors are logged, never fatal.
- [ ] Tests (no network): config merge/skip, skill write/skip, tool path
      validation + send via a fake.
- [ ] Note: opencode loads config/skills once at startup — restart it to pick
      these up.

### Phase 7 - single launcher (opencode serve + bot) (CURRENT)
One command starts both the opencode server and the Telegram bot, so the freshly
installed MCP config/skill are loaded without a separate opencode restart.

- [ ] `telegram_bot/launcher.py` + `telegram_bot/__main__.py`:
      1. run `opencode_setup.install_all()` BEFORE starting the server;
      2. detect a running `opencode serve` on the configured port (health check
         and/or listening socket) and **kill it first** if present;
      3. start `opencode serve --port <port> --hostname <host>` (port/host parsed
         from `OPENCODE_BASE_URL`);
      4. wait until healthy, then run the bot;
      5. terminate the server we started on exit.
- [ ] Config: `OPENCODE_SERVE_COMMAND` (default `opencode`); `psutil` added to
      `requirements.txt` for port/PID detection and killing the process tree.
- [ ] `python -m telegram_bot` is the single command; `python -m telegram_bot.bot`
      still works for users who run `opencode serve` themselves.
- [ ] Tests (no network): URL parse, health/running detection, kill decision,
      command construction (Windows `.cmd` shim handling), and no-op when nothing
      is running.

## Tech Stack

| Concern        | Choice                                                        |
| -------------- | ------------------------------------------------------------- |
| Language       | Python 3.14 (available as `python`)                           |
| Bot framework  | `python-telegram-bot` v22.x (async API), long polling         |
| Persistence    | SQLite via stdlib `sqlite3`                                    |
| STT            | OpenRouter `POST /api/v1/audio/transcriptions` via `httpx`     |
| TTS            | `edge-tts` (`edge_tts.Communicate`, async)                    |
| opencode       | Local server HTTP API at `OPENCODE_BASE_URL` (default `:4096`) |
| Config         | root `.env` loaded with `python-dotenv`                        |
| Env manager    | root `.venv` + root `requirements.txt`                         |
| Tests          | `pytest` + `pytest-asyncio`                                    |

## Directory Layout

```
opencode-voice/
├── AGENTS.md
├── .gitignore
├── .env                    # all secrets (gitignored)
├── .env.example            # documented placeholders
├── requirements.txt
├── config.py               # shared env parsing, paths, constants
├── telegram_bot/
│   ├── __init__.py
│   ├── bot.py              # entrypoint (async main, SSE listener, handlers)
│   ├── database.py         # sqlite schema + accessors
│   ├── storage.py          # media download/save/lookup helpers
│   ├── handlers.py         # command + media + opencode handlers
│   └── README.md
├── opencode_client/
│   ├── __init__.py         # public: OpenCodeClient, OpenCodeError, ...
│   └── client.py           # HTTP + SSE client for the opencode server
├── stt/
│   ├── __init__.py         # public: transcribe, transcribe_bytes
│   └── openrouter.py
├── tts/
│   ├── __init__.py         # public: synthesize, synthesize_bytes
│   └── edge.py
├── data/                   # sqlite db file (gitignored)
├── media/                  # downloaded media (gitignored)
└── tests/
    ├── conftest.py
    ├── test_database.py
    ├── test_storage.py
    ├── test_stt.py
    ├── test_tts.py
    └── test_opencode_client.py
```

## Module Interfaces

`stt` (OpenRouter):

```python
from stt import transcribe, transcribe_bytes

await transcribe(audio_path, *, language=None, model=None) -> str
await transcribe_bytes(data: bytes, audio_format: str, *, language=None, model=None) -> str
```

`tts` (Edge TTS):

```python
from tts import synthesize, synthesize_bytes

await synthesize(text, output_path, *, voice=None, rate=None, volume=None, pitch=None) -> Path
await synthesize_bytes(text, *, voice=None, rate=None, volume=None, pitch=None) -> bytes
```

Defaults come from `TTS_VOICE`, `TTS_RATE`, `TTS_VOLUME`, `TTS_PITCH`.

`opencode_client` (opencode server):

```python
from opencode_client import OpenCodeClient, OpenCodeError

client = OpenCodeClient()  # base_url/directory/timeout default from config

await client.health() -> bool
await client.create_session(*, title=None, agent=None, permission=None, model=None) -> dict
await client.send_prompt(session_id, text, *, agent=None, model=None) -> str   # blocking; returns assistant text
await client.prompt_async(session_id, text, *, agent=None) -> None
await client.abort(session_id) -> None
await client.list_messages(session_id) -> list[dict]
await client.reply_permission(request_id, reply) -> None   # reply in {"once","always","reject"}
await client.reply_question(request_id, answers) -> None
await client.list_models() -> list[dict]        # connected providers: {providerID, modelID, name}
await client.list_sessions(*, directory=None) -> list[dict]
await client.compact_session(session_id) -> None
await client.get_mcp_status(*, directory=None) -> dict        # {server: {"status": ...}}
await client.list_tool_ids(*, directory=None) -> list[str]

async for event in client.stream_events():   # parsed SSE events {id, type, properties}
    ...
```

Endpoints used (all relative to `OPENCODE_BASE_URL`, `directory` as query param):

| Purpose            | Method + path                                    | Body / notes                                        |
| ------------------ | ------------------------------------------------ | --------------------------------------------------- |
| Health             | `GET /api/health`                                | -> `{"healthy": true}`                              |
| Create session     | `POST /session`                                  | `{title, agent, permission?}` -> `Session` (`id`)   |
| Prompt (blocking)  | `POST /session/{id}/message`                     | `{parts:[{type:"text",text}]}` -> `{info,parts}`    |
| Prompt (async)     | `POST /session/{id}/prompt_async`                | `204`                                                |
| Abort              | `POST /session/{id}/abort`                       |                                                      |
| History            | `GET /session/{id}/message`                      | `[{info,parts}]`                                     |
| Reply permission   | `POST /permission/{requestID}/reply`             | `{"reply": "once"\|"always"\|"reject"}`             |
| Events (SSE)       | `GET /event`                                     | `data: {"id","type","properties"}`                   |

The assistant text is the concatenation of `parts` where `type == "text"`.

## Phase 3 Flows

**Text flow**
1. Authenticated user sends a text message (non-command).
2. Bot ensures an opencode session exists for the user (`opencode_sessions`).
3. Bot sends a "working" placeholder, calls `send_prompt`, edits/sends the
   assistant text back (split at Telegram's 4096-char limit).
4. If voice mode is ON, the reply is sent as a voice note plus the text.

**Voice flow**
1. Authenticated user sends a voice note or audio file.
2. Bot downloads it (`storage`) and transcribes with `stt`.
3. Transcript goes to opencode as the prompt (echoed to the user as a caption).
4. Reply is text when voice mode is OFF (STT only, **no TTS**); voice + text when
   voice mode is ON.

**Permission flow**
1. A background SSE listener (`GET /event`) runs for the bot's lifetime.
2. On `permission.asked`, the bot maps `sessionID` → user, sends an inline
   keyboard (Allow once / Always / Reject).
3. On callback, the bot calls `POST /permission/{requestID}/reply`; the agent
   loop resumes and the original prompt completes.

**Concurrency**: serialize prompts per user with an `asyncio.Lock`.

## Data Model

`users` table (Phase 3 adds `voice_mode`):

| Column             | Type    | Notes                                        |
| ------------------ | ------- | -------------------------------------------- |
| `id`               | INTEGER | PK, autoincrement                            |
| `telegram_id`      | INTEGER | UNIQUE, NOT NULL                             |
| `username`         | TEXT    | nullable                                     |
| `first_name`       | TEXT    | nullable                                     |
| `last_name`        | TEXT    | nullable                                     |
| `language_code`    | TEXT    | nullable (e.g. `en`)                         |
| `is_authenticated` | INTEGER | 0/1, default 0                               |
| `role`             | TEXT    | `admin` \| `user`, default `user`            |
| `voice_mode`       | INTEGER | 0/1, default 0 (Phase 3)                     |
| `workdir`          | TEXT    | nullable; opencode working dir (Phase 3.5)    |
| `model_provider`   | TEXT    | nullable; selected providerID (Phase 3.5)     |
| `model_id`         | TEXT    | nullable; selected modelID (Phase 3.5)        |
| `created_at`       | TEXT    | ISO-8601 UTC                                 |
| `updated_at`       | TEXT    | ISO-8601 UTC                                 |

`media` table:

| Column           | Type    | Notes                                  |
| ---------------- | ------- | -------------------------------------- |
| `id`             | INTEGER | PK, autoincrement                      |
| `user_id`        | INTEGER | FK -> users.id                         |
| `telegram_id`    | INTEGER | sender telegram id                     |
| `message_id`     | INTEGER | originating Telegram message id        |
| `media_type`     | TEXT    | photo/video/audio/voice/document/...   |
| `file_id`        | TEXT    | Telegram file id                       |
| `file_unique_id` | TEXT    | Telegram unique file id (dedupe key)   |
| `file_name`      | TEXT    | original filename if any               |
| `mime_type`      | TEXT    | nullable                               |
| `file_size`      | INTEGER | bytes, nullable                        |
| `local_path`     | TEXT    | relative path under `media/`           |
| `created_at`     | TEXT    | ISO-8601 UTC                           |

`opencode_sessions` table (Phase 3):

| Column       | Type    | Notes                                  |
| ------------ | ------- | -------------------------------------- |
| `id`         | INTEGER | PK, autoincrement                      |
| `user_id`    | INTEGER | UNIQUE, FK -> users.id                 |
| `session_id` | TEXT    | opencode session id (`ses_...`)        |
| `directory`  | TEXT    | working directory for this session     |
| `created_at` | TEXT    | ISO-8601 UTC                           |
| `updated_at` | TEXT    | ISO-8601 UTC                           |

Admin seed (idempotent, insert-or-ignore then update role):

```
telegram_id     = <your telegram id>
first_name      = <your first name>
last_name       = <your last name>
language_code   = en
is_authenticated= 1
role            = admin
```

Migrations: `init_db()` must add `users.voice_mode`, `users.workdir`,
`users.model_provider`, and `users.model_id` via `ALTER TABLE ... ADD COLUMN`
when missing (SQLite has no `IF NOT EXISTS` for columns).

## Configuration

All secrets live in the **root** `.env` (gitignored). Keys:

```
# Telegram
TELEGRAM_BOT_TOKEN=<bot token>
ADMIN_USER_ID=<your telegram id>
DB_PATH=data/bot.db
MEDIA_DIR=media
LOG_LEVEL=INFO

# STT (OpenRouter)
STT_BASE_URL=https://openrouter.ai/api/v1
STT_API_KEY=<openrouter key>
STT_MODEL=<openrouter stt model slug>
STT_PROVIDER=api

# TTS (Edge TTS)
TTS_VOICE=en-US-AriaNeural
TTS_RATE=+0%
TTS_VOLUME=+0%
TTS_PITCH=+0Hz

# OpenCode API
OPENCODE_BASE_URL=http://localhost:4096
OPENCODE_DIRECTORY=            # empty -> repo root
OPENCODE_AGENT=build
OPENCODE_TIMEOUT=600           # seconds for a blocking prompt
MEDIA_MAX_MB=20                # max size for inbound/outbound media bridging
```

- `.env.example` contains the same keys with placeholder values.
- `config.py` resolves relative `DB_PATH`/`MEDIA_DIR` against the repo root and
  `OPENCODE_DIRECTORY` falls back to the repo root when empty.
- Never commit `.env`, tokens, or anything under `data/` or `media/`.
- `TELEGRAM_BOT_TOKEN` validation is **lazy** (`config.require_telegram_token()`).

## Bot Commands

| Command        | Notes                                                        |
| -------------- | ------------------------------------------------------------ |
| `/start`       | Greeting + auth state.                                       |
| `/help`        | Command list.                                                |
| `/id`          | Caller's Telegram id.                                        |
| `/whoami`      | Profile, role, auth, voice mode.                             |
| `/voice`       | Show voice mode with ON/OFF buttons.                         |
| `/voice on`    | Enable voice replies (TTS).                                  |
| `/voice off`   | Disable voice replies (TTS never used).                      |
| `/new`         | Start a fresh opencode session for the user.                 |
| `/stop`        | Abort the running opencode prompt.                           |
| `/models`      | List connected-provider models; tap one to set it.           |
| `/session`     | List sessions for the workdir; tap one to switch.             |
| `/workdir`     | Show the workdir and open the directory browser.             |
| `/workdir <path>` | Open the browser at `<path>`; pick "Use this directory".  |
| `/compact`     | Compact (summarize) the active session.                      |
| `/mcp`         | List MCP servers, status, and tool ids.                      |
| `/status`      | Show the status of the ongoing opencode task.                |
| `/list`, `/get <id>` | Browse stored media (items tappable, or by id).         |

opencode features require `is_authenticated = 1`.

## Commands

```powershell
# from repo root
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# run the bot
.venv\Scripts\python -m telegram_bot.bot

# tests
.venv\Scripts\python -m pytest tests
```

## Conventions

- Async everywhere (`async def` handlers/clients, `asyncio.run(main())`).
- No comments unless a behavior is genuinely non-obvious.
- Type hints on public functions.
- Root modules (`config`, `stt`, `tts`, `opencode_client`) are imported as
  top-level packages; run entrypoints from the repo root.
- All DB access goes through `telegram_bot/database.py`; handlers never build
  raw SQL.
- All filesystem paths derived from `config.py`; never hardcode paths.
- SQLite connections opened per-operation with `sqlite3.Row` and WAL mode.
- Media files stored under `media/<telegram_id>/<file_unique_id>.<ext>`.
- Log with the stdlib `logging` module; no `print` for diagnostics.
- Keep secrets out of logs: do not log request URLs that embed the bot token.

## Subagent Workflow Rules

- Delegate implementation work to subagents to preserve the primary context.
- Subagents MUST NOT block on questions. If a decision is ambiguous, pick the
  most reasonable recommendation, document the assumption in the code or in a
  short note, and proceed.
- Prefer a single implementation subagent per cohesive module to avoid file
  conflicts; verify with a separate subagent after implementation.
- Every subagent must report: files created/changed, exact commands run, and
  observed results.

## Definition of Done (Phase 3)

1. `opencode_client` talks to the local server: create session, send prompt
   (blocking), parse assistant text from `parts`, stream SSE, reply to
   permissions. Verified with `httpx.MockTransport` (no network in tests).
2. A text message from an authenticated user is answered by opencode in
   Telegram.
3. A voice note is transcribed (STT), sent to opencode, and answered as text
   (voice mode OFF) or voice + text (voice mode ON). TTS is never called when
   voice mode is OFF.
4. `/voice on|off` persists per user; `/new` resets the session.
5. Unauthenticated users are refused with a clear message.
6. opencode `permission.asked` events produce Telegram inline buttons that,
   when tapped, resolve the request and let the prompt finish.
7. `pytest` passes with no network calls; the bot starts and polls.
8. No secrets are committed.

## Definition of Done (Phase 3.5)

1. `/models` shows connected-provider models as inline buttons; selecting one
   persists `model_provider`/`model_id` and the next prompt uses it.
2. `/session` lists sessions for the user's workdir and setting one changes the
   active session used by subsequent prompts.
3. `/workdir` with no argument shows the current dir; `/workdir <path>` validates
   the directory, persists it, and creates a new session there.4. `/compact` compacts the active session via the opencode API.
5. `/mcp` lists servers, status, and tool ids.
6. The Telegram command menu (`setMyCommands`) and `/help` include the new
   commands.
7. `pytest` passes with no network calls.
