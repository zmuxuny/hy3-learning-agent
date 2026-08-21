#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
requirements_file="backend/requirements.txt"
if [[ -f frontend/package.json ]]; then
  requirements_file="backend/requirements-dev.txt"
fi
.venv/bin/pip install -r "$requirements_file"
if [[ ! -f frontend/dist/index.html ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "missing_release_asset: source checkout requires Node/npm to build frontend/dist." >&2
    exit 3
  fi
  npm --prefix frontend ci
  npm --prefix frontend run build
fi

echo "Setup complete. Copy .env.example to .env, then run ./scripts/start.sh and finish the browser onboarding flow."
