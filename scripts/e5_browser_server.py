"""Run the real UI on a fresh external state root with the shared Hy3 ledger.

Supply OPENAI_API_KEY in the environment. Runtime, planning subagents, titles,
and memory compression all use one prepaid recorder. No external notification
channel or periodic scheduler is configured.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "evaluation/src")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--budget-ledger", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    state = args.state_root.resolve()
    if state == ROOT or ROOT in state.parents:
        raise ValueError("browser state must be outside the checkout")
    state.mkdir(parents=True, exist_ok=False)
    shutil.copytree(ROOT / "frontend/dist", state / "frontend/dist")
    os.environ.update({
        "EVALUATION_MODE": "1", "RUNTIME_STATE_ROOT": str(state),
        "DATABASE_URL": f"sqlite+aiosqlite:///{state}/data/learning_companion.db",
        "ENABLE_SCHEDULER": "false", "ENABLE_EMAIL_REPLY_POLLING": "false",
        "OPENAI_API_BASE": "https://tokenhub.tencentmaas.com/v1", "MODEL_NAME": "hy3",
        "MODEL_CONTEXT_WINDOW": "196608", "AGENT_OUTPUT_TOKEN_RESERVE": "16000",
        "AGENT_MAX_MODEL_CALLS": "8", "AGENT_MAX_TOOL_CALLS": "16",
        "AGENT_MODEL_RETRY_ATTEMPTS": "1", "AGENT_MODEL_TIMEOUT_SECONDS": "120",
        "DEPLOYMENT_MODE": "local",
        **{key: "" for key in ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO", "IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"]},
    })
    from app.core import config
    config.PROJECT_ROOT = state
    import uvicorn
    from app.main import app
    from app.runtime.model_clients import (
        current_model_call_metadata,
        use_model_client_factory,
    )
    from learning_agent_eval.model_budget import OUTPUT_LIMIT, ModelBudget
    from learning_agent_eval.recorder import EvaluationModelRecorder
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=config.settings.OPENAI_API_BASE, max_retries=0)
    recorder = EvaluationModelRecorder(
        client, invocation_mode="real", metadata_provider=current_model_call_metadata,
        max_calls=80, max_output_tokens=OUTPUT_LIMIT,
        budget=ModelBudget(args.budget_ledger), budget_scope="e5-browser-experience",
    )
    # The connection-test API constructs its own SDK client outside Runtime.
    # Bind that explicit seam too, retaining its one-token request limit.
    from app.api import settings as settings_api

    class ConnectionClient:
        def __init__(self, **kwargs):
            if kwargs.get("base_url", "").rstrip("/") != config.settings.OPENAI_API_BASE:
                raise ValueError("browser experiment uses the priced Hy3 endpoint")
            self._client = AsyncOpenAI(**kwargs)
            metered = EvaluationModelRecorder(
                self._client, invocation_mode="real", max_calls=1, max_output_tokens=1,
                budget=ModelBudget(args.budget_ledger), budget_scope="e5-browser-connection",
            )
            self.chat = metered.chat

        async def close(self):
            await self._client.close()

    settings_api.AsyncOpenAI = ConnectionClient
    with use_model_client_factory(lambda: recorder):
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
