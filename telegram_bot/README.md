# telegram_bot

Telegram interface for `opencode-voice`: two-way media sync backed by SQLite,
plus text/voice round trips with a local opencode server (Phase 3).
Project-level configuration lives at the repository root; this directory is an
importable package.

## Setup

From the repository root (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` at the repository root and fill in a real
`TELEGRAM_BOT_TOKEN` and your numeric `ADMIN_USER_ID`.

Configuration keys:

| Key                  | Meaning                                          |
| -------------------- | ------------------------------------------------ |
| `TELEGRAM_BOT_TOKEN` | BotFather token (required, never commit).         |
| `ADMIN_USER_ID`       | Required; your Telegram numeric id, seeded as authenticated admin. |
| `DB_PATH`            | SQLite file, relative to the repository root.     |
| `MEDIA_DIR`          | Media store root, relative to repo root.          |
| `LOG_LEVEL`          | stdlib logging level (e.g. `INFO`).               |
| `STT_BASE_URL`       | OpenRouter base URL for speech-to-text.           |
| `STT_API_KEY`        | OpenRouter key (required for voice input).        |
| `STT_MODEL`          | OpenRouter transcription model slug.              |
| `TTS_VOICE` etc.     | Edge TTS voice/rate/volume/pitch defaults.        |
| `OPENCODE_BASE_URL`  | Local opencode server (default `:4096`).          |
| `OPENCODE_DIRECTORY` | Working directory for opencode sessions.          |
| `OPENCODE_AGENT`     | Agent used for prompts (default `build`).         |
| `OPENCODE_TIMEOUT`   | Seconds for a blocking prompt (default `600`).    |

## Run

The single launcher starts both the opencode server and the bot:

```powershell
.venv\Scripts\python -m telegram_bot
```

It installs the global MCP tool + skill, stops any stale `opencode serve` on the
configured port, starts a fresh `opencode serve --port … --hostname …`, waits for
`GET /global/health`, then runs the bot; the server it started is stopped on
exit. The bot uses long polling, seeds the admin, creates `data/` and `media/`,
and runs a background SSE listener for permission and question events.

If you run your own opencode server, skip the launcher:

```powershell
.venv\Scripts\python -m telegram_bot.bot
```

`python telegram_bot\bot.py` also works via the package's root-path bootstrap.

### Webhook mode

With `TELEGRAM_WEBHOOK_URL` set, the bot runs `run_webhook` instead of polling:
it listens on `127.0.0.1:TELEGRAM_WEBHOOK_PORT/<TELEGRAM_WEBHOOK_PATH>` and
registers `https://<host>/<TELEGRAM_WEBHOOK_PATH>` with Telegram (secret
`TELEGRAM_WEBHOOK_SECRET`). The launcher starts and stops a remotely-managed
cloudflared tunnel (`CLOUDFLARED_COMMAND`, `CLOUDFLARED_TUNNEL_TOKEN`) for the
public HTTPS URL. Empty `TELEGRAM_WEBHOOK_URL` keeps long polling.

### Start/stop/status scripts

For a background launcher managed from the shell, use `scripts\bot.ps1
start|stop|status` (Windows) or `./scripts/bot.sh start|stop|status`
(Linux/macOS). Pid file in `run/bot.pid`, logs in `logs/` (gitignored); `stop`
also stops the opencode server on the configured port and never touches
cloudflared.

## Commands

- `/start`, `/help` - welcome and command list.
- `/id` - your Telegram user id and chat id.
- `/whoami` - your stored profile, role, authentication and voice mode.
- `/voice` - show voice reply mode with `🔊 ON` / `🔇 OFF` buttons.
- `/voice on` - reply with a voice note plus text.
- `/voice off` - reply with text only (TTS is never called).
- `/new` - drop the stored opencode session and start a fresh one.
- `/stop` - abort the running opencode prompt.
- `/models` - list connected-provider models as inline buttons; tapping one
  persists it as the model used by subsequent prompts.
- `/session` - list sessions for the current working directory and switch the
  active one (or start a new session).
- `/workdir` - open the directory browser at the current working directory.
- `/workdir <path>` - open the browser at a validated directory; pick
  `✅ Use this directory` to set it and start a fresh session there.
- `/compact` - compact (summarize) the active session.
- `/mcp` - list MCP servers with their status and the available tool ids.
- `/status` - show the status of the ongoing opencode task.
- `/list` - your 20 most recent stored media, each as a tappable button.
- `/get <id>` - resend a stored media item by its id.

Send text to prompt opencode. Send a voice note or audio file to transcribe it
(STT), echo the transcript, and prompt opencode with it. Any other photo, video,
document, animation, video note, or sticker is stored. Files are saved as
`media/<telegram_id>/<file_unique_id>.<ext>` with a row in the `media` table.
For authenticated users, non-voice media is also forwarded to opencode as a text
prompt referencing the file's absolute local path (with the caption when
present), so opencode's `read` tool can open it; unauthenticated users only get
the storage confirmation.

opencode features require `is_authenticated = 1`; unauthenticated users get an
access-pending message (voice media is still stored first).

## Voice mode

Each user has a persisted `voice_mode` flag. When it is **on**, opencode replies
are sent both as text and as a voice note synthesized with Edge TTS. When it is
**off**, replies are text only and TTS is never invoked. Voice input is always
transcribed regardless of the flag. `/voice` renders `🔊 ON` and `🔇 OFF`
buttons (`voice:on` / `voice:off`) with the active mode marked `✅`; tapping one
persists the flag and edits the message.

## Media buttons (Phase 3.8)

`/list` renders each of the 20 most recent stored media items as inline buttons
(label `📎 <id> · <type>`, callback `media:<id>`, two per row). Tapping a button
sends that file using the same `_send_media` helper as `/get`, including the
same owner/admin gate; `/get <id>` continues to work.

## Outbound media (Phase 4.2)

After a prompt completes, the bot inspects the latest assistant message and the
session diff and sends any produced media back to the user:

- `file` parts are fetched from their `url` (`data:`, `file:`, or `http(s)`; the
  local opencode server is allowed) and sent as a photo when the mime/extension
  is `image/*` (except GIF), otherwise as a document.
- Files created or modified by the task are collected from `patch` parts
  (`files`) and `GET /session/{id}/diff` (`path`), filtered to media extensions
  (`png, jpg, jpeg, gif, webp, bmp, svg, mp4, mov, webm, mkv, mp3, wav, ogg,
  m4a, flac, pdf`), and only sent when they exist inside the session directory
  and are within `MEDIA_MAX_MB`.

Downloads enforce the `MEDIA_MAX_MB` cap (aborting on `Content-Length` or
streamed size). Filesystem paths are rejected unless contained by the session
directory. Sends are deduplicated per session via an in-memory
`SENT_MEDIA: dict[str, set[str]]` so the same file is not sent twice, and all
media work is wrapped so a failure never breaks the text reply.

## Reliable Telegram send (MCP + skill)

In addition to the pull-based method above, an explicit path is installed
globally at bot startup so the agent can deliberately send a file:

- `telegram_bot/send_media.py` is both a CLI and a reusable API:
  `python -m telegram_bot.send_media "<absolute path>" [--caption "..."] [--chat-id ID]`.
  It enforces `MEDIA_MAX_MB`, posts images as photos (except GIF) and everything
  else as documents, and defaults the chat to `ADMIN_USER_ID`.
- `telegram_bot/mcp_server.py` is a local stdio MCP server exposing the
  `telegram_send_file(path, caption?)` tool. It uses the `mcp` SDK's `FastMCP`
  when available and otherwise falls back to a dependency-free JSON-RPC stdio
  server.
- `telegram_bot/opencode_setup.py` installs both, idempotently, on startup:
  it merges an `mcp.telegram` entry into `~/.config/opencode/opencode.json`
  (preserving other keys, skipping invalid JSON) and writes
  `~/.config/opencode/skills/telegram-media/SKILL.md`, which documents the MCP
  tool and the CLI fallback.

opencode reads its config and skills once at startup, so **restart opencode**
after the bot has installed them. The MCP server runs `mcp_server.py` with the
bot's Python interpreter and never writes the bot token into any file.

## Rich text replies (Phase 8)

opencode replies are markdown; the bot converts them to Telegram HTML
(`parse_mode="HTML"`) so bold, italic, strike, headings, links, blockquotes,
lists, inline code, and fenced code blocks render properly. Text is HTML-escaped
first, so raw `<`/`>`/`&` can never inject tags. Replies are split into
≤4096-char chunks before conversion; if Telegram rejects the entities
(`BadRequest`), the same chunk is re-sent as plain text. Voice mode synthesizes
from `markdown_to_plain(reply_text)` (markers and tags stripped), never from the
HTML, while the accompanying text message uses the HTML version.

## Permissions

A background supervisor keeps one SSE listener per distinct session working
directory: `GET /event?directory=<dir>` is directory-scoped, so events for a
session in `D:\Projects\jcp` only arrive on a stream subscribed with that
directory. The supervisor reconciles the set of listeners from
`config.OPENCODE_DIRECTORY` plus every authenticated session's directory
(re-checked every ~10s and immediately when a session/workdir changes), starting
and cancelling listeners as needed.

When opencode asks for permission (`permission.asked`), the bot sends an inline
keyboard with **Allow once**, **Always**, and **Reject**. Tapping a button calls
`POST /permission/{requestID}/reply` and resumes the agent; the original message
is edited to show the decision. Questions (`question.asked`) are likewise
surfaced from whichever directory's stream carries them. Prompts are serialized
per Telegram user with an `asyncio.Lock`.

## Question prompts (Phase 5)

opencode's `question` tool blocks the agent until answered. The SSE listener
handles `question.asked` and, via `database.get_user_by_session_id`, sends the
question to the owning user with inline buttons:

- single-select: one button per option (`q:a:<qidx>:<oidx>`) that submits
  immediately;
- multi-select: toggle buttons (`q:t:<qidx>:<oidx>`, toggled options marked
  `✅`) plus `✅ Done` (`q:d:<qidx>`);
- free text: `✏️ Type answer` (`q:x:<qidx>`) is always available - it sets
  `awaiting_text`, and the next `text_message` is consumed as the answer;
- `🚫 Skip` (`q:r`) rejects the whole request via
  `POST /question/{requestID}/reject`.

Answers are collected in order and submitted with
`POST /question/{requestID}/reply` as `{"answers": [[...], ...]}`. State lives
in `PENDING_QUESTIONS` keyed by Telegram id; `question.replied` /
`question.rejected` events clear it. On startup `reconcile_questions` lists each
stored session's pending questions (`GET /question?directory=...`) so a question
that arrived before a restart is still surfaced. Callback data stays within
Telegram's 64-byte limit.

While a question is pending, plain text messages are never forwarded to
opencode: any typed text is always taken as a free-text answer for the current
question (opencode accepts one-element answer arrays even when `custom` is not
set). The `✏️ Type answer` button is always shown, and a single-select question
hints `Tap an option, or type your own answer.` `/status` re-surfaces the
pending question and its buttons instead of the task status. `present_question`
is re-entrant: reconciling the same `request_id` edits the existing message
rather than sending duplicates, and a newer request replaces the previous
state.

## Sessions

One persistent opencode session is stored per user in `opencode_sessions`
together with its working directory. `/new` deletes the stored mapping so the
next prompt creates a fresh session. `/session` lists sessions for the current
working directory and lets the user switch the active one.

## Command palette (Phase 3.5)

- `/models` fetches connected providers and their models, renders them as
  paginated inline buttons (8 per page) and stores `model_provider` /
  `model_id` per user. The next prompt sends `{"providerID","modelID"}` to
  opencode. Because provider/model ids can exceed Telegram's 64-byte
  `callback_data` limit, buttons carry a short token (`mdl:m0`, `mdl:pg:1`) that
  maps to the full id in memory.
- `/workdir` opens an interactive directory browser (see below).
- `/compact` calls `POST /api/session/{id}/compact` for the active session.
- `/mcp` shows each MCP server with its connection status plus the tool ids
  from `/experimental/tool/ids`.

## Working-directory browser (Phase 3.6)

`/workdir` (no argument) opens the browser at the user's current workdir;
`/workdir <path>` expands `~`, resolves the path, and opens the browser there
(rejecting paths that are not existing absolute directories). The browser shows
the absolute path, the number of subdirectories, and a preview of up to eight
files with the total file count.

Inline buttons:

- `✅ Use this directory` (`wdir:use`) - persists `users.workdir`, creates a new
  opencode session in that directory, and stores it.
- `📁 <name>` (`wdir:o:<index>`) - navigate into a child directory. The index is
  the position within the in-memory child list, so the callback data stays well
  under Telegram's 64-byte limit.
- `⬅️ Prev` / `Next ➡️` (`wdir:pg:<n>`) - page through children (12 per page);
  omitted at the ends.
- `⬆️ Parent` (`wdir:up`) - go to the parent directory; omitted at a filesystem
  root.
- `🔄 Refresh` (`wdir:refresh`) - re-list the current directory.

Browse state is kept in memory in `WORKDIR_BROWSE` keyed by Telegram id (owner,
path, page, children), so it is lost when the bot restarts; pressing a stale
button tells the user to run `/workdir` again. Buttons are bound to the user who
opened the browser; other users are rejected.

## Task status (Phase 3.7)

`/status` reports the ongoing task for the user's active opencode session. It
combines `GET /session/status` (the server's `busy` / `retry` / `idle` state)
with an in-memory `ACTIVE_TASKS` record of the prompt currently being awaited
(session id, prompt, start time). A task is considered ongoing when the server
reports `busy`/`retry` for that session or a local task is in flight.

The reply shows the session id, the status (`busy`, `retry (attempt N): ...`, or
`running` when only the local task exists), the elapsed time (or `Elapsed
unknown`), the prompt, and the session todo list from
`GET /session/{id}/todo` with per-item icons (`⬜ pending`, `🔄 in_progress`,
`✅ completed`, `❌ cancelled`) plus a `done/total` summary. When nothing is
running it replies `No ongoing task found.`

`ACTIVE_TASKS` is set immediately before the blocking prompt call and cleared in
a `finally`, so both the text and voice flows are covered and it is always reset
on success or error. The reply also includes an `Activity:` line derived from the
last meaningful part of the latest assistant message (`reasoning`, `text`, or
`tool:<name>`), which is useful because this server's `/session/status` stays
empty for v1 sessions while busy.

The bot runs PTB with `concurrent_updates(True)` so `/status`, permission
callbacks, and other commands are processed while a prompt is being awaited;
prompts remain serialized per user by the existing `asyncio.Lock`.

## Tests

```powershell
.venv\Scripts\python -m pytest tests
```

Tests use fakes and `httpx.MockTransport`; they never make network calls.

## Related modules

- `stt/` - OpenRouter speech-to-text client (`transcribe`, `transcribe_bytes`).
- `tts/` - Edge TTS client (`synthesize`, `synthesize_bytes`).
- `opencode_client/` - async HTTP/SSE client for the local opencode server.

All are importable from the repository root and wired into the bot.
