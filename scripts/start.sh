#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run ./scripts/setup.sh first." >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and configure OPENAI_API_KEY." >&2
  exit 1
fi
if [[ ! -f frontend/dist/index.html ]]; then
  npm --prefix frontend run build
fi

exec .venv/bin/python backend/run.py
