#!/usr/bin/env bash
set -euo pipefail

cd /home/ubuntu/trading-bots

echo "== Pulling latest code =="
git pull

echo "== Checking dependency changes =="
if git rev-parse -q --verify 'HEAD@{1}' >/dev/null 2>&1; then
  if git diff --name-only HEAD@{1} HEAD | grep -q '^requirements\.txt$'; then
    echo "== requirements.txt changed; installing deps =="
    /home/ubuntu/trading-bots/.venv/bin/pip install -r requirements.txt
  else
    echo "== requirements.txt unchanged; skipping pip install =="
  fi
else
  echo "== No previous HEAD (fresh repo state); skipping diff check =="
fi

echo "== Restarting enabled trading-bot@ instances =="

# Enabled units show up as symlinks in multi-user.target.wants
mapfile -t LIVE_UNITS < <(
  find /etc/systemd/system/multi-user.target.wants \
    -maxdepth 1 -type l -name 'trading-bot@*.service' -printf '%f\n' \
    | sort
)

mapfile -t PRACTICE_UNITS < <(
  find /etc/systemd/system/multi-user.target.wants \
    -maxdepth 1 -type l -name 'trading-bot-practice@*.service' -printf '%f\n' \
    | sort
)

ALL_UNITS=("${LIVE_UNITS[@]}" "${PRACTICE_UNITS[@]}")

if [ "${#ALL_UNITS[@]}" -eq 0 ]; then
  echo "No enabled trading-bot@*.service units found. Nothing to restart."
  exit 0
fi

for unit in "${ALL_UNITS[@]}"; do
  echo "Restarting: $unit"
  sudo systemctl restart "$unit"
done

echo "== Status (enabled units) =="
systemctl --no-pager --full status "${ALL_UNITS[@]}" | sed -n '1,120p'
