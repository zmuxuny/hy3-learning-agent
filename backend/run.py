import argparse
import sys
from pathlib import Path

import uvicorn

BACKEND_ROOT = Path(__file__).resolve().parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402 - path is established above
from app.core.deployment import (  # noqa: E402
    DeploymentConfigError,
    validate_bind_host,
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local Learning Agent service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    try:
        validate_bind_host(settings.deployment_policy, args.host)
    except DeploymentConfigError as exc:
        parser.error(str(exc))
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
