#!/bin/zsh
set -u

PORT="${OCR_WEB_PORT:-7860}"

PIDS="$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
if [ -z "$PIDS" ]; then
  echo "No OCR web server is running on port ${PORT}."
  echo ""
  echo "Press Enter to close this window..."
  read -r _
  exit 0
fi

echo "Stopping OCR web server on port ${PORT}..."
for PID in $PIDS; do
  kill "$PID" 2>/dev/null || true
done

sleep 1
if lsof -i "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Still running. Trying force stop..."
  for PID in $PIDS; do
    kill -9 "$PID" 2>/dev/null || true
  done
fi

if lsof -i "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Failed to stop process on port ${PORT}."
else
  echo "Stopped."
fi

echo ""
echo "Press Enter to close this window..."
read -r _
