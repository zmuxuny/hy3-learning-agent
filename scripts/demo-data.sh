#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

usage() {
  echo "Usage: $0 reset | restore <backup-directory>" >&2
}

require_stopped_service() {
  if curl --silent --fail --max-time 1 http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
    echo "Learning Agent is still running on 127.0.0.1:8000." >&2
    echo "Stop ./scripts/start.sh before changing the SQLite demo state." >&2
    exit 2
  fi
}

new_backup_dir() {
  local label="$1"
  local timestamp
  timestamp="$(date +%Y%m%d-%H%M%S)"
  echo "$project_dir/data/backups/${label}-${timestamp}"
}

move_runtime_state() {
  local destination="$1"
  local moved=0
  mkdir -p "$destination"
  for relative_path in \
    data/learning_companion.db \
    data/learning_companion.db-shm \
    data/learning_companion.db-wal \
    data/context \
    data/workspace
  do
    if [[ -e "$relative_path" ]]; then
      mv "$relative_path" "$destination/"
      moved=1
    fi
  done
  echo "$moved"
}

prepare_empty_runtime_dirs() {
  mkdir -p data/context/plans data/context/decisions data/workspace
}

command_name="${1:-}"
case "$command_name" in
  reset)
    require_stopped_service
    backup_dir="$(new_backup_dir pre-demo)"
    moved="$(move_runtime_state "$backup_dir")"
    prepare_empty_runtime_dirs
    if [[ "$moved" == "1" ]]; then
      echo "Previous local state moved to: $backup_dir"
    else
      rmdir "$backup_dir"
      echo "No previous local state was present."
    fi
    echo "Demo state is empty. Run ./scripts/start.sh to create the local owner and profile."
    ;;
  restore)
    require_stopped_service
    source_dir="${2:-}"
    if [[ -z "$source_dir" ]]; then
      usage
      exit 1
    fi
    source_dir="$(realpath "$source_dir")"
    backups_root="$(realpath "$project_dir/data/backups")"
    if [[ "$source_dir" != "$backups_root"/* || ! -d "$source_dir" ]]; then
      echo "Restore source must be an existing directory inside data/backups/." >&2
      exit 2
    fi

    current_backup="$(new_backup_dir before-restore)"
    moved="$(move_runtime_state "$current_backup")"
    if [[ "$moved" == "0" ]]; then
      rmdir "$current_backup"
    else
      echo "Current local state moved to: $current_backup"
    fi

    for name in learning_companion.db learning_companion.db-shm learning_companion.db-wal context workspace; do
      if [[ -e "$source_dir/$name" ]]; then
        cp -a "$source_dir/$name" "data/$name"
      fi
    done
    prepare_empty_runtime_dirs
    echo "Restored demo state from: $source_dir"
    ;;
  *)
    usage
    exit 1
    ;;
esac
