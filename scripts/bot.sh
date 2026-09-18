#!/usr/bin/env bash
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
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/global/health" >/dev/null 2>&1; then
        healthy="healthy"
      fi
    else
      if "$PY" - "$PORT" <<'PYEOF' >/dev/null 2>&1
import sys
import urllib.request

port = int(sys.argv[1])
try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/global/health", timeout=3
    ) as response:
        sys.exit(0 if 200 <= response.status < 300 else 1)
except Exception:
    sys.exit(1)
PYEOF
      then
        healthy="healthy"
      fi
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
