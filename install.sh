#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -d ".venv" ] && ! grep -q "include-system-site-packages = true" .venv/pyvenv.cfg 2>/dev/null; then
  rm -rf .venv
fi

python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt

if ! command -v shairport-sync >/dev/null 2>&1; then
  echo "shairport-sync is required. Install it with: sudo apt install shairport-sync"
fi

if [ -S "${WHISPLAY_DAEMON_SOCKET_PATH:-/tmp/whisplay-daemon.sock}" ]; then
  python3 register.py || true
fi

echo "Install complete. Launch with: ./run.sh"
