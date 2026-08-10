#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="/home/pi/subway-audio"

if [[ "${USER}" != "pi" ]]; then
  echo "This installer is prepared for user 'pi'. Current user: ${USER}" >&2
  exit 1
fi

echo "[1/7] Installing OS packages..."
sudo apt update
sudo apt install -y alsa-utils python3 python3-venv python3-pip curl

echo "[2/7] Preparing repository at ${TARGET_DIR}..."
if [[ "${REPO_DIR}" != "${TARGET_DIR}" ]]; then
  mkdir -p "${TARGET_DIR}"
  cp -a "${REPO_DIR}/." "${TARGET_DIR}/"
fi
cd "${TARGET_DIR}"

echo "[3/7] Creating Python virtual environment..."
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "[4/7] Creating runtime directories..."
mkdir -p queue control runtime/raw

if [[ ! -f config.env ]]; then
  cp config.env.example config.env
  chmod 600 config.env
  echo
  echo "IMPORTANT: edit ${TARGET_DIR}/config.env before starting the service."
fi

echo "[5/7] Installing control command..."
sudo install -m 0755 scripts/subway-audioctl /usr/local/bin/subway-audioctl

echo "[6/7] Installing systemd service..."
sudo install -m 0644 systemd/subway-audio.service /etc/systemd/system/subway-audio.service
sudo systemctl daemon-reload
sudo systemctl enable subway-audio.service

echo "[7/7] Done."
echo
cat <<'EOF'
Next steps:
  1. nano /home/pi/subway-audio/config.env
  2. arecord -l
  3. sudo systemctl start subway-audio.service
  4. sudo systemctl status subway-audio.service --no-pager -l
  5. subway-audioctl start
  6. speak for several seconds
  7. subway-audioctl stop
  8. subway-audioctl logs
EOF
