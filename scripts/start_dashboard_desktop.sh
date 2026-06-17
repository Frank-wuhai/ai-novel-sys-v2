#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/frank/ai-novel-system-v2"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"

cd "$ROOT"

# Restart any previous local dashboard from this project so the HTML is reloaded.
pkill -f "$ROOT/scripts/run_local_dashboard.py" >/dev/null 2>&1 || true
pkill -f "scripts/run_local_dashboard.py --host 127.0.0.1" >/dev/null 2>&1 || true
sleep 0.5

for port in 8765 8766 8767 8768 8769; do
  log_file="$LOG_DIR/dashboard-$port.log"
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$ROOT/venv/bin/python" "$ROOT/scripts/run_local_dashboard.py" --host 127.0.0.1 --port "$port" >"$log_file" 2>&1 < /dev/null &
  else
    nohup "$ROOT/venv/bin/python" "$ROOT/scripts/run_local_dashboard.py" --host 127.0.0.1 --port "$port" >"$log_file" 2>&1 < /dev/null &
  fi
  pid=$!
  sleep 1
  if kill -0 "$pid" >/dev/null 2>&1; then
    url="http://127.0.0.1:$port"
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$url" >/dev/null 2>&1 || true
    fi
    printf "AI Novel Dashboard started: %s\nLog: %s\n" "$url" "$log_file"
    exit 0
  fi
done

printf "Failed to start dashboard. Check logs in %s\n" "$LOG_DIR" >&2
exit 1
