"""Run a synthetic product scenario, then resume only an explicitly reviewed approval.

All state stays outside the checkout. This is a product experience, not a
Benchmark runner: no approval policy is injected into formal test cases.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "evaluation/src"), str(ROOT / "backend")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "approve", "reject"])
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--budget-ledger", type=Path, required=True)
    parser.add_argument("--approval-sha256")
    args = parser.parse_args()
    state = args.state_root.resolve()
    if state == ROOT or ROOT in state.parents:
        raise ValueError("product probe requires an external state root")
    if args.action == "start":
        state.mkdir(parents=True, exist_ok=False)
    elif not args.approval_sha256 or not (state / "data/learning_companion.db").exists():
        raise ValueError("resume requires reviewed approval digest and existing state")
    case = json.loads(args.case.read_text())
    os.environ.update({
        "EVALUATION_MODE": "1", "RUNTIME_STATE_ROOT": str(state),
        "DATABASE_URL": f"sqlite+aiosqlite:///{state}/data/learning_companion.db",
        "DEFAULT_OWNER_ID": case["runtime_setup"]["owner_id"], "DEFAULT_TIMEZONE": case["runtime_setup"]["timezone"],
        "ENABLE_SCHEDULER": "false", "ENABLE_EMAIL_REPLY_POLLING": "false",
        "MODEL_CONTEXT_WINDOW": "196608", "AGENT_OUTPUT_TOKEN_RESERVE": "16000",
        "AGENT_MAX_MODEL_CALLS": "8", "AGENT_MAX_TOOL_CALLS": "16", "AGENT_MODEL_RETRY_ATTEMPTS": "1",
        **{k: "" for k in ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO", "IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"]},
    })
    asyncio.run(execute(args, state, case))


async def execute(args, state, case):
    from app.core import config
    from app.core.time import frozen_utc, utc_now
    from app.db.database import AsyncSessionLocal, create_schema, engine
    from app.models import (
        AgentRun,
        Operation,
        Plan,
        PlanProposal,
        RunApproval,
        TaskSubmission,
        ToolInvocation,
    )
    from app.runtime.agent import AgentRuntime
    from app.runtime.model_clients import (
        current_model_call_metadata,
        use_model_client_factory,
    )
    from app.runtime.state import decide_approval
    from app.search import use_snapshot_provider
    from learning_agent_eval.canonical import canonical_json_bytes, sha256_digest
    from learning_agent_eval.case_specs import legacy_runtime_projection_v2
    from learning_agent_eval.model_budget import ModelBudget
    from learning_agent_eval.recorder import EvaluationModelRecorder, public_projection
    from learning_agent_eval.resources import EvaluationSnapshotProvider
    from learning_agent_eval.runtime_fixture import _seed_fixture
    from openai import AsyncOpenAI
    from sqlalchemy import select

    config.PROJECT_ROOT = state
    config.prepare_runtime_directories(state)
    fixture = legacy_runtime_projection_v2(case)
    frozen = datetime.fromisoformat(fixture["frozen_time"].replace("Z", "+00:00"))
    await create_schema(state_root=state)
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=config.settings.OPENAI_API_BASE, max_retries=0)
    class Recorder(EvaluationModelRecorder):
        def _publish(self, record):
            ticket = record.get("_budget_ticket")
            super()._publish(record)
            row = {**public_projection(record), "budget_ticket": ticket}
            with (state / "public-calls.jsonl").open("ab") as handle:
                handle.write(canonical_json_bytes(row) + b"\n")
    recorder = Recorder(client, invocation_mode="real", metadata_provider=current_model_call_metadata,
                        max_calls=16, max_output_tokens=16000, budget=ModelBudget(args.budget_ledger), budget_scope="e6-explicit-approval-experience")
    with frozen_utc(frozen), use_snapshot_provider(EvaluationSnapshotProvider(args.resources)), use_model_client_factory(lambda: recorder):
        if args.action == "start":
            await _seed_fixture(fixture, utc_now())
        else:
            async with AsyncSessionLocal() as db:
                run = await db.get(AgentRun, fixture["run_id"])
                pending = run.pending_approval
                if run.status != "waiting_approval" or not pending or sha256_digest(pending["tool_call"]) != args.approval_sha256:
                    raise ValueError("pending action differs from the reviewed approval")
                await decide_approval(db, run.id, owner_id=fixture["owner_id"], approved=args.action == "approve", note="Explicit approval by primary AI acting as synthetic learner after reviewing this exact action.", answer=None)
        await AgentRuntime().run(fixture["run_id"], resume=args.action != "start")
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, fixture["run_id"])
            pending = run.pending_approval
            facts = {"role": "primary_ai_synthetic_learner", "formal": False, "stage": args.action,
                     "status": run.status, "output": run.output, "pending_approval": pending,
                     "approval_sha256": sha256_digest(pending["tool_call"]) if pending else None,
                     "reviewed_approval_sha256": args.approval_sha256, "model_calls": len(recorder.records)}
            for name, model, fields in [
                ("plans", Plan, ["id", "title", "version"]),
                ("proposals", PlanProposal, ["id", "status", "plan_payload"]),
                ("submissions", TaskSubmission, ["id", "status", "score", "feedback"]),
                ("operations", Operation, ["id", "tool_name", "forward_patch", "inverse_patch"]),
                ("approvals", RunApproval, ["id", "decision", "tool_name", "tool_call_id"]),
                ("invocations", ToolInvocation, ["tool_name", "status", "canonical_args", "result_payload"]),
            ]:
                records = (await db.execute(select(model).where(model.owner_id == fixture["owner_id"]))).scalars()
                facts[name] = [{field: getattr(row, field) for field in fields} for row in records]
            path = state / f"facts-{args.action}-{len(list(state.glob('facts-*.json'))):02d}.json"
            path.write_bytes(canonical_json_bytes(public_projection(facts)))
            print(json.dumps({"facts": str(path), "status": run.status, "approval_sha256": facts["approval_sha256"]}), flush=True)
    await client.close()
    await engine.dispose()


if __name__ == "__main__":
    main()
