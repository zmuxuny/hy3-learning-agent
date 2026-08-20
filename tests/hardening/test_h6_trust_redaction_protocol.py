from __future__ import annotations

import io
import hashlib
import json
import logging
import secrets
from datetime import timedelta

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select
from uvicorn.logging import AccessFormatter

import app.api.settings as settings_api
from app.context.assembler import ContextAssembler
from app.context.provenance import canonical_digest
from app.core.config import settings
from app.core.redaction import REDACTED, SecretRedactionFilter, redact_data, redact_event_fields
from app.core.time import utc_now
from app.core.trust import (
    authorize_side_effect,
    inspect_run_authority,
    mark_external_untrusted_result,
)
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    ContextSnapshotBlock,
    Plan,
    RunApproval,
    RunEvent,
    Session,
    ToolInvocation,
    UserProfile,
)
from app.runtime.agent import _compact_tool_message, _event_tool_arguments
from app.runtime.checkpoints import make_checkpoint
from app.runtime.events import emit_event, stage_event, subscribe_stream, unsubscribe_stream
from app.runtime.state import claim_run, pause_for_approval, terminate_run
from app.tools import ToolContext, execute_tool
from app.tools.registry import TOOL_MAP, _append_atomic_completion


def _credential() -> str:
    # An opaque runtime value ensures the implementation cannot pass by
    # recognizing a fixture name or a hard-coded sentinel convention.
    return secrets.token_urlsafe(32)


def _contains(value: object, credential: str) -> bool:
    return credential in json.dumps(value, ensure_ascii=False, default=str)


def test_recursive_redaction_uses_live_string_and_secretstr_configuration(monkeypatch):
    credential = _credential()
    server_credential = _credential()
    unknown_bearer = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)
    monkeypatch.setattr(settings, "SERVER_AUTH_TOKEN", SecretStr(server_credential))

    payload = {
        "plain": f"prefix {credential} suffix",
        "nested": [
            {"password": _credential()},
            {"opaque": f"Authorization: Bearer {unknown_bearer}"},
            {"server": server_credential},
        ],
        "encoded_json": json.dumps({"api_key": credential}),
    }
    redacted = redact_data(payload)

    assert not _contains(redacted, credential)
    assert not _contains(redacted, server_credential)
    assert not _contains(redacted, unknown_bearer)
    assert redacted["nested"][0]["password"] == REDACTED


def test_logging_factory_covers_child_logger_and_late_handler(monkeypatch):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)
    stream = io.StringIO()
    logger = logging.getLogger(f"learning-agent.h6.{secrets.token_hex(8)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    # The handler is intentionally installed after runtime.events configured
    # the process-wide LogRecordFactory and has no explicit filter.
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError(credential)
        except RuntimeError:
            logger.exception("provider Authorization: Bearer %s", credential)
    finally:
        logger.removeHandler(handler)
    rendered = stream.getvalue()
    assert credential not in rendered
    assert REDACTED in rendered
    assert isinstance(SecretRedactionFilter(), logging.Filter)


def test_logging_redaction_preserves_structured_formatter_arguments(monkeypatch):
    credential = "h7-browser-log-secret"
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:4321", "GET", "/api/v1/profile", "1.1", 200),
        exc_info=None,
    )
    redaction_filter = SecretRedactionFilter()

    assert redaction_filter.filter(record)
    assert len(record.args) == 5
    assert record.getMessage() == '127.0.0.1:4321 - "GET /api/v1/profile HTTP/1.1" 200'
    rendered_access = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s'
    ).format(record)
    assert rendered_access == '127.0.0.1:4321 - "GET /api/v1/profile HTTP/1.1" 200 OK'

    secret_record = logging.LogRecord(
        name="provider",
        level=logging.ERROR,
        pathname=__file__,
        lineno=2,
        msg="provider token=%s",
        args=(credential,),
        exc_info=None,
    )
    assert redaction_filter.filter(secret_record)
    assert secret_record.args == (REDACTED,)
    assert credential not in secret_record.getMessage()


def test_runtime_tool_trace_and_model_observation_share_recursive_redaction(monkeypatch):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)

    arguments = _event_tool_arguments(
        json.dumps({"input": [{"api_key": credential}], "plain": credential})
    )
    observation = _compact_tool_message(
        {"ok": False, "error": f"Authorization: Bearer {credential}"}
    )

    assert not _contains(arguments, credential)
    assert credential not in observation


@pytest.mark.asyncio
async def test_emit_stage_and_sse_redact_summary_and_nested_payload(monkeypatch):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="redaction")
        db.add(run)
        await db.commit()
        queue = await subscribe_stream(run.id)
        try:
            emitted = await emit_event(
                db,
                run.id,
                "fixture.failed",
                f"provider failed {credential}",
                {"nested": [{"token": credential}], "message": credential},
            )
            streamed = queue.get_nowait()
        finally:
            unsubscribe_stream(run.id, queue)
        staged = await stage_event(
            db,
            run.id,
            "fixture.staged",
            credential,
            {"technical_error": credential},
        )
        await db.commit()
        rows = list(
            (
                await db.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run.id)
                    .order_by(RunEvent.sequence)
                )
            ).scalars()
        )

    assert credential not in emitted.summary
    assert not _contains(emitted.payload, credential)
    assert not _contains(streamed, credential)
    assert credential not in staged.summary
    assert all(credential not in row.summary and not _contains(row.payload, credential) for row in rows)


def test_atomic_event_boundary_api_redacts_before_direct_orm_construction(monkeypatch):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)

    summary, payload = redact_event_fields(
        f"completed {credential}",
        {"result": {"authorization": f"Bearer {credential}"}},
    )

    assert credential not in summary
    assert not _contains(payload, credential)


@pytest.mark.asyncio
async def test_atomic_tool_completion_persists_only_redacted_event(monkeypatch):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)
    now = utc_now()
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="atomic event")
        db.add(run)
        await db.flush()
        invocation = ToolInvocation(
            owner_id="local",
            run_id=run.id,
            idempotency_key="h6:atomic-redaction",
            tool_name="task_patch",
            tool_call_id="atomic-call",
            args_hash="d" * 64,
            request_digest="d" * 64,
            canonical_args={"fixture": True},
            effect_kind="database_write",
            status="running",
            claim_token="atomic-redaction-claim",
            claimed_at=now,
            claim_expires_at=now + timedelta(minutes=1),
            version=1,
        )
        db.add(invocation)
        await db.commit()
        event = await _append_atomic_completion(
            invocation_id=invocation.id,
            claim_token="atomic-redaction-claim",
            claim_version=1,
            name="task_patch",
            data={"technical_error": credential},
            ctx=ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="user_message",
                tool_call_id="atomic-call",
            ),
        )

    assert not _contains(event.payload, credential)


@pytest.mark.asyncio
async def test_context_snapshot_and_projection_digest_only_redacted_content(
    monkeypatch,
    isolated_runtime_root,
):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)
    async with AsyncSessionLocal() as db:
        profile = await db.get(UserProfile, "local")
        assert profile is not None
        profile.agent_style = credential
        profile.preferences = {"api_key": credential, "nested": [credential]}
        run = AgentRun(owner_id="local", trigger="user_message", objective="context redaction")
        db.add(run)
        await db.commit()
        snapshot = await ContextAssembler(db).build(
            "local",
            run_id=run.id,
            objective="context redaction",
            prompt_system="bounded system prompt",
            prompt_tools=[],
        )
        snapshot_id = snapshot.id
        run_id = run.id
        await db.commit()
        blocks = list(
            (
                await db.execute(
                    select(ContextSnapshotBlock).where(
                        ContextSnapshotBlock.snapshot_id == snapshot_id
                    )
                )
            ).scalars()
        )

    run_markdown = (
        isolated_runtime_root / "data" / "context" / "runs" / f"{run_id}.md"
    ).read_text(encoding="utf-8")
    global_markdown = (
        isolated_runtime_root / "data" / "context" / "global.md"
    ).read_text(encoding="utf-8")
    assert credential not in snapshot.markdown
    assert not _contains(snapshot.source_manifest, credential)
    assert credential not in run_markdown
    assert credential not in global_markdown
    assert snapshot.context_digest == canonical_digest(snapshot.markdown)
    assert snapshot.source_digest == canonical_digest(
        {
            "retained": snapshot.source_manifest,
            "dropped": snapshot.dropped_source_manifest,
        }
    )
    assert blocks
    assert all(len(block.source_digest) == 64 and len(block.block_digest) == 64 for block in blocks)


@pytest.mark.asyncio
async def test_settings_diagnostic_redacts_configured_provider_error(monkeypatch):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)

    async def fail_smtp(*, send_message=False):
        del send_message
        raise RuntimeError(credential)

    monkeypatch.setattr(settings_api, "test_smtp", fail_smtp)
    with pytest.raises(HTTPException) as captured:
        await settings_api.test_email_configuration(
            settings_api.EmailTestRequest(channel="smtp")
        )

    assert credential not in str(captured.value.detail)


@pytest.mark.asyncio
async def test_external_read_authority_survives_restart_and_child_ancestry():
    digest = "a" * 64
    async with AsyncSessionLocal() as db:
        parent = AgentRun(owner_id="local", trigger="user_message", objective="parent")
        db.add(parent)
        await db.flush()
        parent_id = parent.id
        child = AgentRun(
            owner_id="local",
            trigger="subagent",
            objective="child",
            parent_run_id=parent_id,
        )
        db.add(child)
        await db.flush()
        db.add(
            ToolInvocation(
                owner_id="local",
                run_id=parent_id,
                idempotency_key="h6:external-read",
                tool_name="web_open",
                tool_call_id="read-call",
                args_hash=digest,
                request_digest=digest,
                canonical_args={"url": "https://example.invalid/"},
                effect_kind="external_read",
                status="committed",
                result_payload=mark_external_untrusted_result({"content": "fixture"}),
                completed_at=utc_now(),
            )
        )
        await db.commit()
        child_id = child.id

    # A new AsyncSession represents process restart/recovery: no in-memory
    # taint object is available, so the durable invocation and parent link are
    # the only authority source.
    async with AsyncSessionLocal() as restarted_db:
        authority = await inspect_run_authority(
            restarted_db,
            child_id,
            owner_id="local",
        )

    assert authority.structurally_valid
    assert authority.external_untrusted
    assert authority.source_run_ids == (parent_id,)
    assert "external_read_result" in authority.reason_codes


def _plan_payload(title: str) -> dict[str, object]:
    return {
        "title": title,
        "goal": "Exercise the exact durable authority protocol",
        "current_level": "fixture",
        "weekly_minutes": 60,
        "expected_outcome": "One approved durable write",
        "stages": [
            {
                "title": "Fixture stage",
                "tasks": [{"title": "Fixture task"}],
            }
        ],
    }


@pytest.mark.asyncio
async def test_committed_file_read_blocks_following_write_without_prompt_trust(
    isolated_runtime_root,
):
    workspace = isolated_runtime_root / "data" / "workspace"
    (workspace / "authority.txt").write_text("external fixture", encoding="utf-8")
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="authority")
        db.add(run)
        await db.commit()
        read_result = await execute_tool(
            "file_read",
            json.dumps({"path": "authority.txt"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="user_message",
                tool_call_id="external-read-call",
            ),
        )
        write_result = await execute_tool(
            "plan_create",
            json.dumps(_plan_payload("Must remain pending")),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="user_message",
                tool_call_id="blocked-write-call",
            ),
        )
        plan_count = int(await db.scalar(select(func.count(Plan.id))) or 0)

    assert read_result.get("ok") is True
    assert read_result.get("data", {}).get("external_untrusted") is True
    assert write_result.get("status") == "pending_approval"
    assert write_result.get("data", {}).get("approval_required") is True
    assert plan_count == 0


@pytest.mark.asyncio
async def test_registry_centrally_marks_external_content_when_handler_omits_marker(
    monkeypatch,
):
    async def unmarked_file_read(_ctx, _args):
        return {
            "path": "unmarked-fixture.txt",
            "content": "external fixture",
            "truncated": False,
            "size": 16,
        }

    monkeypatch.setattr(TOOL_MAP["file_read"], "handler", unmarked_file_read)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="central marker")
        db.add(run)
        await db.commit()
        result = await execute_tool(
            "file_read",
            json.dumps({"path": "unmarked-fixture.txt"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="user_message",
                tool_call_id="central-marker-call",
            ),
        )
        invocation = (
            await db.execute(
                select(ToolInvocation).where(
                    ToolInvocation.run_id == run.id,
                    ToolInvocation.tool_call_id == "central-marker-call",
                )
            )
        ).scalars().one()

    assert result["data"]["external_untrusted"] is True
    assert invocation.result_payload["external_untrusted"] is True


@pytest.mark.asyncio
async def test_child_external_content_taints_parent_status_before_write():
    digest = "f" * 64
    async with AsyncSessionLocal() as db:
        parent = AgentRun(owner_id="local", trigger="user_message", objective="parent")
        db.add(parent)
        await db.flush()
        child = AgentRun(
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="external research",
            status="completed",
            phase="terminal",
            output="external child report",
            completed_at=utc_now(),
        )
        db.add(child)
        await db.flush()
        db.add(
            ToolInvocation(
                owner_id="local",
                run_id=child.id,
                idempotency_key="h6:child-external-read",
                tool_name="web_open",
                tool_call_id="child-web-call",
                args_hash=digest,
                request_digest=digest,
                canonical_args={"url": "https://example.invalid/"},
                effect_kind="external_read",
                status="committed",
                result_payload=mark_external_untrusted_result({"content": "fixture"}),
                completed_at=utc_now(),
            )
        )
        await db.commit()

        status_result = await execute_tool(
            "subagent_status",
            json.dumps({"run_id": child.id}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=parent.id,
                trigger="user_message",
                tool_call_id="parent-child-status",
            ),
        )
        write_result = await execute_tool(
            "plan_create",
            json.dumps(_plan_payload("Child report must not authorize")),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=parent.id,
                trigger="user_message",
                approval_granted=True,
                tool_call_id="parent-write-after-child",
            ),
        )
        invocation = (
            await db.execute(
                select(ToolInvocation).where(
                    ToolInvocation.run_id == parent.id,
                    ToolInvocation.tool_call_id == "parent-write-after-child",
                )
            )
        ).scalars().one()
        now = utc_now()
        approval = RunApproval(
            owner_id="local",
            run_id=parent.id,
            invocation_id=invocation.id,
            tool_call_id="parent-write-after-child",
            tool_name="plan_create",
            tool_call={
                "id": "parent-write-after-child",
                "name": "plan_create",
                "arguments": "{}",
            },
            remaining_tool_calls=[],
            reason="must approve the full child-derived request",
            decision="approve",
            decided_at=now,
            consumed_at=now,
        )
        db.add(approval)
        await db.commit()
        subset_approval = await execute_tool(
            "plan_create",
            json.dumps(_plan_payload("Child report must not authorize")),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=parent.id,
                trigger="user_message",
                approval_granted=True,
                tool_call_id="parent-write-after-child",
            ),
        )
        count_after_subset = int(await db.scalar(select(func.count(Plan.id))) or 0)
        approval.tool_call = {
            "id": "parent-write-after-child",
            "name": "plan_create",
            "arguments": json.dumps(
                invocation.canonical_args,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        await db.commit()
        exact_approval = await execute_tool(
            "plan_create",
            json.dumps(_plan_payload("Child report must not authorize")),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=parent.id,
                trigger="user_message",
                approval_granted=True,
                tool_call_id="parent-write-after-child",
            ),
        )
        count_after_exact = int(await db.scalar(select(func.count(Plan.id))) or 0)

    assert status_result["data"]["external_untrusted"] is True
    assert write_result.get("status") == "pending_approval"
    assert subset_approval.get("status") == "pending_approval"
    assert subset_approval.get("data", {}).get("reason_code") == "approval_projection_mismatch"
    assert count_after_subset == 0
    assert exact_approval.get("ok") is True
    assert exact_approval.get("status") == "committed"
    assert count_after_exact == 1


@pytest.mark.asyncio
async def test_planning_child_external_content_taints_parent_delegate_result(
    monkeypatch,
):
    import app.tools.planning as planning_tools

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "synthetic-planning-provider-key")

    async def external_child(child_id: str, **_kwargs) -> None:
        digest = hashlib.sha256(child_id.encode("utf-8")).hexdigest()
        async with AsyncSessionLocal() as child_db:
            child = await child_db.get(AgentRun, child_id)
            assert child is not None
            child_db.add(
                ToolInvocation(
                    owner_id=child.owner_id,
                    run_id=child.id,
                    idempotency_key=f"h6:planning-child:{child.id}",
                    tool_name="web_search",
                    tool_call_id="planning-child-web",
                    args_hash=digest,
                    request_digest=digest,
                    canonical_args={"query": "external fixture"},
                    effect_kind="external_read",
                    status="committed",
                    result_payload=mark_external_untrusted_result(
                        {"results": ["external fixture"]}
                    ),
                    completed_at=utc_now(),
                )
            )
            await child_db.commit()
        await terminate_run(
            AsyncSessionLocal,
            child_id,
            status="failed",
            reason_code="synthetic_child_complete",
            summary="synthetic planning child completed",
        )

    monkeypatch.setattr(planning_tools, "_run_planning_child", external_child)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="Planning taint")
        db.add(session)
        await db.flush()
        parent = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="delegate external research",
            status="running",
        )
        db.add(parent)
        await db.commit()
        delegated = await execute_tool(
            "planning_delegate",
            json.dumps(
                {
                    "assignments": [
                        {"role": "research", "objective": "inspect external material"}
                    ]
                }
            ),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=parent.id,
                trigger="user_message",
                session_id=session.id,
                tool_call_id="planning-external-delegate",
            ),
        )
        write_result = await execute_tool(
            "plan_create",
            json.dumps(_plan_payload("Planning report must not authorize")),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=parent.id,
                trigger="user_message",
                session_id=session.id,
                tool_call_id="planning-write-after-child",
            ),
        )
        plan_count = int(await db.scalar(select(func.count(Plan.id))) or 0)

    assert delegated["data"]["external_untrusted"] is True
    assert delegated["data"]["reports"][0]["external_untrusted"] is True
    assert write_result.get("status") == "pending_approval"
    assert plan_count == 0


@pytest.mark.asyncio
async def test_registry_return_and_durable_failure_payload_are_redacted(monkeypatch):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)

    async def failed_file_read(_ctx, _args):
        return {"error": f"provider failed with {credential}"}

    monkeypatch.setattr(TOOL_MAP["file_read"], "handler", failed_file_read)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="failure redaction")
        db.add(run)
        await db.commit()
        result = await execute_tool(
            "file_read",
            json.dumps({"path": "safe-fixture.txt"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="user_message",
                tool_call_id="redacted-failure-call",
            ),
        )
        invocation = (
            await db.execute(
                select(ToolInvocation).where(
                    ToolInvocation.run_id == run.id,
                    ToolInvocation.tool_call_id == "redacted-failure-call",
                )
            )
        ).scalars().one()

    assert not _contains(result, credential)
    assert not _contains(invocation.result_payload, credential)


@pytest.mark.asyncio
async def test_registry_redacts_validated_success_before_return_and_persistence(
    monkeypatch,
):
    credential = _credential()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)

    async def successful_file_read(_ctx, _args):
        return mark_external_untrusted_result(
            {
                "path": "safe-fixture.txt",
                "content": f"external provider returned {credential}",
                "truncated": False,
                "size": len(credential),
            }
        )

    monkeypatch.setattr(TOOL_MAP["file_read"], "handler", successful_file_read)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="success redaction")
        db.add(run)
        await db.commit()
        result = await execute_tool(
            "file_read",
            json.dumps({"path": "safe-fixture.txt"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="user_message",
                tool_call_id="redacted-success-call",
            ),
        )
        invocation = (
            await db.execute(
                select(ToolInvocation).where(
                    ToolInvocation.run_id == run.id,
                    ToolInvocation.tool_call_id == "redacted-success-call",
                )
            )
        ).scalars().one()

    assert result["ok"] is True
    assert result["data"]["external_untrusted"] is True
    assert invocation.result_payload["external_untrusted"] is True
    assert not _contains(result, credential)
    assert not _contains(invocation.result_payload, credential)


@pytest.mark.asyncio
async def test_registry_uses_exact_durable_approval_not_boolean_only():
    raw_arguments = json.dumps(_plan_payload("Approved exact plan"))
    now = utc_now()
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="email_reply", objective="email authority")
        db.add(run)
        await db.commit()
        initial = await execute_tool(
            "plan_create",
            raw_arguments,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="email_reply",
                tool_call_id="approved-email-write",
            ),
        )
        invocation = (
            await db.execute(
                select(ToolInvocation).where(
                    ToolInvocation.run_id == run.id,
                    ToolInvocation.tool_call_id == "approved-email-write",
                )
            )
        ).scalars().one()
        boolean_only = await execute_tool(
            "plan_create",
            raw_arguments,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="email_reply",
                approval_granted=True,
                tool_call_id="approved-email-write",
            ),
        )
        invocation = await db.get(ToolInvocation, invocation.id, populate_existing=True)
        assert invocation is not None
        db.add(
            RunApproval(
                owner_id="local",
                run_id=run.id,
                invocation_id=invocation.id,
                tool_call_id="approved-email-write",
                tool_name="plan_create",
                tool_call={
                    "id": "approved-email-write",
                    "name": "plan_create",
                    "arguments": json.dumps(
                        invocation.canonical_args,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
                remaining_tool_calls=[],
                reason="exact request",
                decision="approve",
                decided_at=now,
                consumed_at=now,
            )
        )
        await db.commit()
        approved = await execute_tool(
            "plan_create",
            raw_arguments,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run.id,
                trigger="email_reply",
                approval_granted=True,
                tool_call_id="approved-email-write",
            ),
        )
        plan_count = int(await db.scalar(select(func.count(Plan.id))) or 0)

    assert initial.get("status") == "pending_approval"
    assert boolean_only.get("status") == "pending_approval"
    assert approved.get("ok") is True and approved.get("status") == "committed"
    assert plan_count == 1


@pytest.mark.asyncio
async def test_pause_projects_fully_defaulted_invocation_arguments():
    raw_arguments = json.dumps({"title": "Canonical approval projection"})
    checkpoint = make_checkpoint(
        kind="agent",
        phase="tool_ready",
        step=0,
        messages=[],
        current_tool_call={
            "id": "canonical-approval-call",
            "name": "plan_create",
            "arguments": raw_arguments,
        },
    )
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="email_reply",
            objective="canonical approval",
            checkpoint_schema_version=1,
            checkpoint=checkpoint,
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    lease = await claim_run(AsyncSessionLocal, run_id)
    assert lease is not None

    async with AsyncSessionLocal() as db:
        initial = await execute_tool(
            "plan_create",
            raw_arguments,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="email_reply",
                tool_call_id="canonical-approval-call",
            ),
        )
        assert initial.get("status") == "pending_approval"
        await pause_for_approval(
            db,
            lease,
            checkpoint=lease.checkpoint or checkpoint,
            tool_call={
                "id": "canonical-approval-call",
                "name": "plan_create",
                "arguments": raw_arguments,
            },
            remaining_tool_calls=[],
            reason="approve the exact canonical request",
        )
        invocation = (
            await db.execute(
                select(ToolInvocation).where(
                    ToolInvocation.run_id == run_id,
                    ToolInvocation.tool_call_id == "canonical-approval-call",
                )
            )
        ).scalars().one()
        approval = (
            await db.execute(
                select(RunApproval).where(
                    RunApproval.run_id == run_id,
                    RunApproval.tool_call_id == "canonical-approval-call",
                )
            )
        ).scalars().one()

    projected = json.loads(approval.tool_call["arguments"])
    assert projected == invocation.canonical_args
    assert projected != json.loads(raw_arguments)
    assert projected["weekly_minutes"] == 0
    assert projected["stages"] == []


@pytest.mark.asyncio
async def test_email_authority_requires_exact_consumed_approval_and_live_claim():
    canonical_args = {"title": "approved title"}
    encoded = json.dumps(canonical_args, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    now = utc_now()
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="email_reply", objective="external email")
        db.add(run)
        await db.flush()
        invocation = ToolInvocation(
            owner_id="local",
            run_id=run.id,
            idempotency_key="h6:approved-write",
            tool_name="plan_create",
            tool_call_id="write-call",
            args_hash=digest,
            request_digest=digest,
            canonical_args=canonical_args,
            effect_kind="database_write",
            status="running",
            claim_token="h6-live-claim",
            claimed_at=now,
            claim_expires_at=now + timedelta(minutes=1),
            version=3,
        )
        db.add(invocation)
        await db.flush()
        approval = RunApproval(
            owner_id="local",
            run_id=run.id,
            invocation_id=invocation.id,
            tool_call_id="write-call",
            tool_name="plan_create",
            tool_call={
                "id": "write-call",
                "name": "plan_create",
                "arguments": json.dumps(canonical_args),
            },
            remaining_tool_calls=[],
            reason="exact external authority",
            decision="approve",
            decided_at=now,
            consumed_at=now,
        )
        db.add(approval)
        await db.commit()

        approval.tool_call = {
            "id": "write-call",
            "name": "plan_create",
            "arguments": "{}",
        }
        await db.commit()
        omitted_projection = await authorize_side_effect(
            db,
            owner_id="local",
            run_id=run.id,
            tool_call_id="write-call",
            tool_name="plan_create",
            effect_kind="database_write",
            request_digest=digest,
            invocation_id=invocation.id,
            claim_token="h6-live-claim",
            claim_version=3,
            canonical_args=canonical_args,
        )
        approval.tool_call = {
            "id": "write-call",
            "name": "plan_create",
            "arguments": json.dumps(canonical_args),
        }
        await db.commit()
        allowed = await authorize_side_effect(
            db,
            owner_id="local",
            run_id=run.id,
            tool_call_id="write-call",
            tool_name="plan_create",
            effect_kind="database_write",
            request_digest=digest,
            invocation_id=invocation.id,
            claim_token="h6-live-claim",
            claim_version=3,
            canonical_args=canonical_args,
        )
        wrong_request = await authorize_side_effect(
            db,
            owner_id="local",
            run_id=run.id,
            tool_call_id="write-call",
            tool_name="plan_create",
            effect_kind="database_write",
            request_digest="b" * 64,
            invocation_id=invocation.id,
            claim_token="h6-live-claim",
            claim_version=3,
            canonical_args=canonical_args,
        )
        wrong_claim = await authorize_side_effect(
            db,
            owner_id="local",
            run_id=run.id,
            tool_call_id="write-call",
            tool_name="plan_create",
            effect_kind="database_write",
            request_digest=digest,
            invocation_id=invocation.id,
            claim_token="not-the-owner",
            claim_version=3,
            canonical_args=canonical_args,
        )

    assert not omitted_projection.allowed and omitted_projection.requires_approval
    assert omitted_projection.reason_code == "approval_projection_mismatch"
    assert allowed.allowed and not allowed.requires_approval
    assert allowed.reason_code == "approved_exact_request"
    assert not wrong_request.allowed and wrong_request.requires_approval
    assert wrong_request.reason_code == "approval_request_mismatch"
    assert not wrong_claim.allowed and wrong_claim.requires_approval
    assert wrong_claim.reason_code == "approval_claim_mismatch"


@pytest.mark.asyncio
async def test_structurally_invalid_run_authority_is_not_approval_overridable():
    async with AsyncSessionLocal() as db:
        decision = await authorize_side_effect(
            db,
            owner_id="local",
            run_id="missing-run",
            tool_call_id="call",
            tool_name="plan_create",
            effect_kind="database_write",
            request_digest="c" * 64,
        )

    assert not decision.allowed
    assert not decision.requires_approval
    assert decision.reason_code == "run_missing"
