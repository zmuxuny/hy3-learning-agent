"""Retired worker module; historical execution is disabled."""

from __future__ import annotations

import json
import sys

from .legacy import LegacyExecutionDisabledError


def main() -> int:
    error = LegacyExecutionDisabledError("python -m learning_agent_eval.worker_v3")
    print(json.dumps(error.as_dict(), sort_keys=True, separators=(",", ":")), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
