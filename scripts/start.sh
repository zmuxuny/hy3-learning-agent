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
if curl --silent --fail --max-time 2 http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
  echo "Learning Agent is already running on 127.0.0.1:8000." >&2
  echo "Stop the existing instance first; two servers sharing one SQLite file cause data corruption." >&2
  exit 2
fi
if [[ ! -f frontend/dist/index.html ]]; then
  npm --prefix frontend run build
fi

exec .venv/bin/python backend/run.py
