#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if curl --silent --fail --max-time 1 http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
  echo "Learning Agent is still running on 127.0.0.1:8000." >&2
  echo "Stop ./scripts/start.sh before restoring a fixture." >&2
  exit 2
fi

latest_backup="$(ls -1d data/backups/pre-demo-* 2>/dev/null | sort | tail -n 1 || true)"
if [[ -z "$latest_backup" || ! -f "$latest_backup/learning_companion.db" ]]; then
  echo "No fixture backup found under data/backups/pre-demo-*." >&2
  echo "Run ./scripts/reset-data.sh first to back up the current state." >&2
  exit 1
fi

current_backup="data/backups/before-restore-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$current_backup"
moved=0
for relative_path in \
  data/learning_companion.db data/learning_companion.db-shm data/learning_companion.db-wal \
  data/context data/workspace \
  learning_companion.db learning_companion.db-shm learning_companion.db-wal \
  backend/learning_companion.db backend/learning_companion.db-shm backend/learning_companion.db-wal \
  backend/data/learning_companion.db backend/data/learning_companion.db-shm backend/data/learning_companion.db-wal
do
  if [[ -e "$relative_path" ]]; then
    mv "$relative_path" "$current_backup/"
    moved=1
  fi
done
if [[ "$moved" == "0" ]]; then
  rmdir "$current_backup"
fi

for name in learning_companion.db learning_companion.db-shm learning_companion.db-wal context workspace; do
  if [[ -e "$latest_backup/$name" ]]; then
    cp -a "$latest_backup/$name" "data/$name"
  fi
done
chmod 644 data/learning_companion.db 2>/dev/null || true
mkdir -p data/context/plans data/context/decisions data/workspace
echo "Restored browser-regression fixture from: $latest_backup"
