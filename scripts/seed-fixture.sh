#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

case "$#" in
  0)
    exec python3 scripts/data-maintenance.py restore --latest-purpose pre_clean
    ;;
  1)
    exec python3 scripts/data-maintenance.py restore --backup "$1"
    ;;
  *)
    echo "Usage: $0 [backup-directory]" >&2
    exit 1
    ;;
esac
