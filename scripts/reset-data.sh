#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ "$#" -ne 0 ]]; then
  echo "Usage: $0" >&2
  exit 1
fi

exec python3 scripts/data-maintenance.py reset --purpose pre_clean
