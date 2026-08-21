#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

old_head="$(git rev-parse HEAD)"
branch="$(git rev-parse --abbrev-ref HEAD)"

git fetch origin "$branch"
git pull --ff-only origin "$branch"

new_head="$(git rev-parse HEAD)"

if [[ "$old_head" == "$new_head" ]]; then
  echo "No new commits pulled; skipping bot restarts."
  exit 0
fi

changed_files="$(git diff --name-only "$old_head" "$new_head")"

runtime_change_pattern='^(bots/|shared/|config/|requirements\.txt$|systemd/|.*\.service$|.*\.timer$)'

if ! grep -Eq "$runtime_change_pattern" <<<"$changed_files"; then
  echo "Pulled changes are non-runtime (e.g., tools/notebooks/README); skipping bot restarts."
  exit 0
fi

mapfile -t bot_units < <(systemctl list-units --type=service --all --no-legend "trading-bot@*.service" | awk '{print $1}')

if [[ "${#bot_units[@]}" -eq 0 ]]; then
  echo "No trading-bot@ units found; nothing to restart."
  exit 0
fi

echo "Runtime changes detected; restarting trading-bot@ units:"
printf ' - %s\n' "${bot_units[@]}"
systemctl restart "${bot_units[@]}"

