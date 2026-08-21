#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

old_head="$(git rev-parse HEAD)"
branch="$(git rev-parse --abbrev-ref HEAD)"

echo "== Pulling latest code =="
git fetch origin "$branch"
git pull --ff-only origin "$branch"

new_head="$(git rev-parse HEAD)"

if [[ "$old_head" == "$new_head" ]]; then
  echo "No new commits pulled; skipping dependency check and bot restarts."
  exit 0
fi

changed_files="$(git diff --name-only "$old_head" "$new_head")"

echo "== Checking dependency changes =="
if grep -q '^requirements\.txt$' <<<"$changed_files"; then
  echo "== requirements.txt changed; installing deps =="
  "$REPO_DIR/.venv/bin/pip" install -r requirements.txt
else
  echo "== requirements.txt unchanged; skipping pip install =="
fi

runtime_change_pattern='^(bots/|shared/|config/|requirements\.txt$|deploy/|.*\.service$|.*\.timer$)'

if ! grep -Eq "$runtime_change_pattern" <<<"$changed_files"; then
  echo "Pulled changes are non-runtime (e.g., tools/notebooks/README); skipping bot restarts."
  exit 0
fi

echo "== Restarting enabled trading-bot@ / trading-bot-practice@ instances =="

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
  echo "No enabled trading-bot@*/trading-bot-practice@* units found. Nothing to restart."
  exit 0
fi

echo "Runtime changes detected; restarting:"
printf ' - %s\n' "${ALL_UNITS[@]}"

for unit in "${ALL_UNITS[@]}"; do
  sudo systemctl restart "$unit"
done

echo "== Status (enabled units) =="
systemctl --no-pager --full status "${ALL_UNITS[@]}" | sed -n '1,120p'
