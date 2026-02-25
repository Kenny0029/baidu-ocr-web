#!/bin/zsh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  /usr/bin/osascript -e 'display alert "OCR Web 启动失败" message "未找到 python3，请先安装 Python 3。"' || true
  exit 1
fi

if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi
if [ ! -f ".venv/bin/activate" ]; then
  /usr/bin/osascript -e 'display alert "OCR Web 启动失败" message "虚拟环境损坏（.venv/bin/activate 缺失）。"' || true
  exit 1
fi

source .venv/bin/activate
python -m pip install -r requirements.txt >/tmp/ocr_web_install.log 2>&1 || {
  /usr/bin/osascript -e 'display alert "OCR Web 启动失败" message "依赖安装失败，请查看 /tmp/ocr_web_install.log。"' || true
  exit 1
}

pick_port() {
  local p
  for p in "$@"; do
    if ! lsof -i "tcp:${p}" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

is_ocr_service() {
  local p="$1"
  curl -fsS "http://127.0.0.1:${p}/" 2>/dev/null | grep -q "文澜 OCR 工坊"
}

BASE_PORT="${OCR_WEB_PORT:-7860}"
TARGET_PORT="$BASE_PORT"

if lsof -i "tcp:${TARGET_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  if is_ocr_service "$TARGET_PORT"; then
    /usr/bin/open "http://127.0.0.1:${TARGET_PORT}" >/dev/null 2>&1 || true
    exit 0
  fi
  TARGET_PORT="$(pick_port 7861 7862 7863 7864 7865 7866 7867 7868 7869 7870 || true)"
fi

if [ -z "${TARGET_PORT:-}" ]; then
  /usr/bin/osascript -e 'display alert "OCR Web 启动失败" message "7860-7870 端口都被占用，请释放端口后重试。"' || true
  exit 1
fi

export OCR_WEB_HOST="127.0.0.1"
export OCR_WEB_PORT="$TARGET_PORT"
export OCR_WEB_AUTO_OPEN="1"

python web_app.py
