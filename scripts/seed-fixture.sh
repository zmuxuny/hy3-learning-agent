#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

latest_backup="$(ls -1d data/backups/pre-demo-* 2>/dev/null | sort | tail -n 1 || true)"
if [[ -z "$latest_backup" || ! -f "$latest_backup/learning_companion.db" ]]; then
  echo "No fixture backup found under data/backups/pre-demo-*." >&2
  echo "Run ./scripts/demo-data.sh reset first to back up the current state, or provide a backup." >&2
  exit 1
fi

echo "Restoring browser-regression fixture from: $latest_backup"
exec ./scripts/demo-data.sh restore "$latest_backup"
