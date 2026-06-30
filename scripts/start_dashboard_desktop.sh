#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/frank/ai-novel-system-v2"
LOG_DIR="$ROOT/data/logs"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8765}"
URL="http://$HOST:$PORT"
mkdir -p "$LOG_DIR"

cd "$ROOT"

# Restart any previous local dashboard from this project so the HTML is reloaded.
pkill -f "$ROOT/scripts/run_local_dashboard.py" >/dev/null 2>&1 || true
pkill -f "scripts/run_local_dashboard.py --host 127.0.0.1" >/dev/null 2>&1 || true
sleep 0.5

log_file="$LOG_DIR/dashboard-$PORT.log"
if command -v setsid >/dev/null 2>&1; then
  setsid nohup "$ROOT/venv/bin/python" "$ROOT/scripts/run_local_dashboard.py" --host "$HOST" --port "$PORT" >"$log_file" 2>&1 < /dev/null &
else
  nohup "$ROOT/venv/bin/python" "$ROOT/scripts/run_local_dashboard.py" --host "$HOST" --port "$PORT" >"$log_file" 2>&1 < /dev/null &
fi
pid=$!
sleep 1
if kill -0 "$pid" >/dev/null 2>&1; then
  printf "%s\n" "$URL" >"$ROOT/data/dashboard-url.txt"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  fi
  printf "AI Novel Dashboard started: %s\nLog: %s\n" "$URL" "$log_file"
  exit 0
fi

printf "Failed to start dashboard at fixed URL %s.\n" "$URL" >&2
printf "The port may be occupied. Check log: %s\n" "$log_file" >&2
tail -n 20 "$log_file" >&2 || true
exit 1
