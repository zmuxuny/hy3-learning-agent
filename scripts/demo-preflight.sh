#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run ./scripts/setup.sh first." >&2
  exit 1
fi

echo "[1/6] Backend tests"
.venv/bin/pytest -q

echo "[2/6] Frontend production build"
npm --prefix frontend run build

echo "[3/6] Frontend production dependency audit"
npm --prefix frontend audit --omit=dev

echo "[4/6] Patch whitespace validation"
git diff --check

echo "[5/6] Tracked data and secret guard"
tracked_private="$(git ls-files -- .env .env.local 'data/**' '*.db' '*.sqlite3')"
if [[ -n "$tracked_private" ]]; then
  echo "Refusing release: private runtime files are tracked:" >&2
  echo "$tracked_private" >&2
  exit 2
fi
if git grep -nE 'sk-[A-Za-z0-9_-]{20,}|(SMTP|IMAP)_(PASSWORD|AUTHORIZATION_CODE)=[A-Za-z0-9+/=_-]{16,}' -- ':!package-lock.json'; then
  echo "Refusing release: a likely credential exists in tracked files." >&2
  exit 2
fi

echo "[6/6] Optional running-service diagnostics"
if curl --silent --fail --max-time 2 http://127.0.0.1:8000/api/v1/health >/dev/null; then
  .venv/bin/python - <<'PY'
import json
import urllib.request

base = "http://127.0.0.1:8000/api/v1"
with urllib.request.urlopen(f"{base}/health", timeout=3) as response:
    health = json.load(response)
with urllib.request.urlopen(f"{base}/settings", timeout=3) as response:
    settings = json.load(response)
with urllib.request.urlopen(f"{base}/settings/tools", timeout=3) as response:
    tools = json.load(response)

print(
    json.dumps(
        {
            "health": health.get("status"),
            "model": settings.get("model"),
            "hy3_key_configured": settings.get("api_key_configured"),
            "scheduler_enabled": settings.get("scheduler_enabled"),
            "email_configured": settings.get("email_configured"),
            "email_reply_configured": settings.get("email_reply_configured"),
            "typed_tool_count": tools.get("count"),
        },
        ensure_ascii=False,
        indent=2,
    )
)
PY
else
  echo "Service is not running; HTTP diagnostics skipped."
fi

echo "Demo preflight passed. Real Hy3 and SMTP/IMAP calls remain explicit rehearsal checks."
