#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

usage() {
  echo "Usage: $0 reset | restore <backup-directory>" >&2
}

command_name="${1:-}"
case "$command_name" in
  reset)
    if [[ "$#" -ne 1 ]]; then
      usage
      exit 1
    fi
    exec python3 scripts/data-maintenance.py reset --purpose pre_demo
    ;;
  restore)
    if [[ "$#" -ne 2 ]]; then
      usage
      exit 1
    fi
    exec python3 scripts/data-maintenance.py \
      restore \
      --backup "$2" \
      --expected-purpose pre_demo
    ;;
  *)
    usage
    exit 1
    ;;
esac
