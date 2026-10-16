#!/usr/bin/env bash
#
# Manage the opencode-voice launcher (opencode v2 server + Telegram bot).
#
#   start  - launch `python -m telegram_bot` in the background; it restarts
#            opencode serve on the configured port (with OPENCODE_SERVER_PASSWORD
#            from .env) and then runs the bot. Logs go to logs/.
#   stop   - stop the launcher and every opencode server listening on the port.
#   status - report launcher, listener, and /api/info health (with basic auth).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="$ROOT/logs"
RUN="$ROOT/run"
PIDFILE="$RUN/bot.pid"

resolve_python() {
  if [ -x "$ROOT/.venv/bin/python" ]; then
    printf '%s' "$ROOT/.venv/bin/python"
  else
    printf '%s' "python3"
  fi
}
PY="$(resolve_python)"

resolve_port() {
  local out=""
  out="$("$PY" -c 'import config,urllib.parse as u; p=u.urlparse(config.OPENCODE_BASE_URL); print(p.port or 4096)' 2>/dev/null || true)"
  if [[ "$out" =~ ^[0-9]+$ ]]; then
    printf '%s' "$out"
  else
    printf '%s' "4096"
  fi
}
PORT="$(resolve_port)"

read_pid() {
  if [ -f "$PIDFILE" ]; then
    tr -d '[:space:]' < "$PIDFILE"
  fi
}

is_alive() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

listener_pids() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$port" 2>/dev/null || true
    return
  fi
  "$PY" - "$port" <<'PYEOF' 2>/dev/null || true
import sys

try:
    import psutil
except Exception:
    sys.exit(0)

port = int(sys.argv[1])
for connection in psutil.net_connections(kind="tcp"):
    laddr = getattr(connection, "laddr", None)
    if (
        laddr
        and laddr.port == port
        and connection.status == psutil.CONN_LISTEN
        and connection.pid
    ):
        print(connection.pid)
PYEOF
}

ACTION="${1:-status}"

case "$ACTION" in
  start)
    pid="$(read_pid)"
    if is_alive "$pid"; then
      echo "opencode-voice already running (pid $pid)"
      exit 0
    fi
    mkdir -p "$LOGS" "$RUN"
    ( cd "$ROOT" && nohup "$PY" -m telegram_bot >>"$LOGS/bot.err.log" 2>&1 & echo $! > "$PIDFILE" )
    echo "Started opencode-voice (pid $(read_pid))."
    echo "Logs: $LOGS/bot.err.log"
    exit 0
    ;;
  stop)
    pid="$(read_pid)"
    if is_alive "$pid"; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 15); do
        if ! is_alive "$pid"; then
          break
        fi
        sleep 1
      done
      if is_alive "$pid"; then
        kill -9 "$pid" 2>/dev/null || true
      fi
      echo "Stopped bot (pid $pid)."
    else
      echo "Bot is not running."
    fi
    rm -f "$PIDFILE"

    pids="$(listener_pids "$PORT")"
    if [ -n "$pids" ]; then
      for listener in $pids; do
        kill "$listener" 2>/dev/null || true
        echo "Stopped opencode server on port $PORT (pid $listener)."
      done
    else
      echo "No opencode server listening on port $PORT."
    fi
    exit 0
    ;;
  status)
    pid="$(read_pid)"
    running="no"
    if is_alive "$pid"; then
      running="yes"
    fi
    listen_pids="$(listener_pids "$PORT")"
    listening="no"
    if [ -n "$listen_pids" ]; then
      listening="yes"
    fi
    healthy="down"
    if ( cd "$ROOT" && "$PY" - "$PORT" <<'PYEOF'
import os, sys

sys.path.insert(0, os.getcwd())
import httpx
import config

creds = config.opencode_server_credentials()
auth = httpx.BasicAuth(*creds) if creds else None
port = sys.argv[1]
for host in ("localhost", "127.0.0.1"):
    try:
        response = httpx.get(f"http://{host}:{port}/api/info", auth=auth, timeout=3)
        if response.status_code == 200:
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PYEOF
    ) >/dev/null 2>&1; then
      healthy="healthy"
    fi

    if [ "$running" = "yes" ]; then
      echo "launcher: running (pid $pid)"
    else
      echo "launcher: not running"
    fi
    if [ "$listening" = "yes" ]; then
      echo "opencode server: listening on port $PORT"
    else
      echo "opencode server: not listening on port $PORT"
    fi
    echo "health: $healthy"

    if [ "$running" = "yes" ] || [ "$listening" = "yes" ] || [ "$healthy" = "healthy" ]; then
      exit 0
    fi
    exit 1
    ;;
  *)
    echo "Usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
