#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if curl --silent --fail --max-time 1 http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
  echo "Learning Agent is still running on 127.0.0.1:8000." >&2
  echo "Stop ./scripts/start.sh before resetting local data." >&2
  exit 2
fi

backup_dir="data/backups/pre-clean-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
moved=0
for relative_path in \
  data/learning_companion.db data/learning_companion.db-shm data/learning_companion.db-wal \
  data/context data/workspace \
  learning_companion.db learning_companion.db-shm learning_companion.db-wal \
  backend/learning_companion.db backend/learning_companion.db-shm backend/learning_companion.db-wal \
  backend/data/learning_companion.db backend/data/learning_companion.db-shm backend/data/learning_companion.db-wal
do
  if [[ -e "$relative_path" ]]; then
    mv "$relative_path" "$backup_dir/"
    moved=1
  fi
done
mkdir -p data/context/plans data/context/decisions data/workspace
if [[ "$moved" == "1" ]]; then
  echo "Local state moved to: $backup_dir"
else
  rmdir "$backup_dir"
  echo "No local state was present."
fi
echo "Data is empty. Restart ./scripts/start.sh to create a fresh owner and profile."
