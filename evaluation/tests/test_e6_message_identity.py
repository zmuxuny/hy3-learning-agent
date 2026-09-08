"""Conversation message IDs must never alias notification delivery IDs."""
from types import SimpleNamespace

import pytest

from learning_agent_eval.normalizers import IdentityCandidate, NormalizationError, StableIdentityRegistry
from learning_agent_eval.snapshots import _data, _register_identities, normalize_reference_fields


def registry_with_messages(message_id=2, notification_id=1):
    registry = StableIdentityRegistry(episode_id="message-identity-probe")
    rows = {
        "session": [SimpleNamespace(id="session", title="public synthetic", created_at="2026-09-09T02:00:00Z")],
        "chat_message": [SimpleNamespace(id=message_id, session_id="session", run_id=None,
            role="assistant", content="Please confirm your time budget.", content_hash="a" * 64,
            version=1, created_at="2026-09-09T02:00:00Z")],
    }
    _register_identities(registry, rows)
    registry.register_many("notification", [IdentityCandidate(raw_id=notification_id, semantic_key={"channel": "in_app"})])
    return registry, rows["chat_message"][0]


@pytest.mark.parametrize("message_id,notification_id", [(2, 1), (101, 7), (1, 1)])
def test_canonical_message_and_delivery_keep_distinct_semantics(message_id, notification_id):
    registry, message = registry_with_messages(message_id, notification_id)
    payload = normalize_reference_fields({"canonical_message_id": message_id,
        "notifications": [{"id": notification_id, "channel": "in_app", "status": "sent"}]}, registry)
    assert payload["canonical_message_ref"] == "chat_message:message-identity-probe:001"
    assert payload["notifications"][0]["notification_ref"] == "notification:message-identity-probe:001"
    assert payload["canonical_message_ref"] != payload["notifications"][0]["notification_ref"]
    assert _data("chat_message", message, registry)["content"] == "Please confirm your time budget."


def test_unknown_message_does_not_fall_back_to_equal_notification_id():
    registry, _ = registry_with_messages(2, 1)
    with pytest.raises(NormalizationError, match="identity.unregistered_reference"):
        normalize_reference_fields({"canonical_message_id": 1}, registry)


@pytest.mark.asyncio
async def test_snapshot_collects_only_intervention_canonical_chat_message(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "messages.sqlite3"
    monkeypatch.setenv("EVALUATION_MODE", "1")
    monkeypatch.setenv("ENABLE_SCHEDULER", "false")
    monkeypatch.setenv("ENABLE_EMAIL_REPLY_POLLING", "false")
    monkeypatch.setenv("RUNTIME_STATE_ROOT", str(runtime))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    from app.db.database import Base
    from app.models import Owner, UserProfile, Session, AgentRun, ChatMessage, Intervention
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from learning_agent_eval.snapshots import collect_state_snapshot

    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        db.add(Owner(id="local", display_name="Synthetic", timezone="Asia/Shanghai"))
        db.add(UserProfile(owner_id="local"))
        db.add(Session(id="conversation", owner_id="local", title="Synthetic"))
        db.add(AgentRun(id="probe", owner_id="local", session_id="conversation", trigger="user_message",
                        objective="Ask for a time budget", status="completed", phase="terminal"))
        await db.flush()
        db.add_all([
            ChatMessage(id=1, session_id="conversation", role="user", content="Ordinary chat must not be exported.", content_hash="b" * 64),
            ChatMessage(id=2, session_id="conversation", run_id="probe", role="assistant", content="Please confirm minutes.", content_hash="a" * 64),
        ])
        await db.flush()
        db.add(Intervention(id="intervention", owner_id="local", session_id="conversation", source_run_id="probe",
                            canonical_message_id=2, title="Question", body="Please confirm minutes.", content_digest="a" * 64,
                            state="legacy_unverified"))
        await db.commit()
    fixture = {"episode_id": "probe", "owner_id": "local", "run_id": "probe", "state_before": {
        "logical_entities": [], "context": {"public_summary": "Synthetic", "source_refs": [], "context_sha256": "0" * 64}}}
    registry = StableIdentityRegistry(episode_id="probe")
    capture = await collect_state_snapshot(factory, fixture, registry=registry,
        captured_at="2026-09-09T02:00:00Z", resource_version="synthetic-v1", resource_digest="0" * 64, phase="after")
    messages = [e for e in capture.document["logical_entities"] if e["entity_type"] == "chat_message"]
    interventions = [e for e in capture.document["logical_entities"] if e["entity_type"] == "intervention"]
    assert len(messages) == 1 and messages[0]["data"]["content"] == "Please confirm minutes."
    assert interventions[0]["data"]["canonical_message_ref"] == messages[0]["logical_id"]
    assert normalize_reference_fields({"canonical_message_id": 2}, registry)["canonical_message_ref"] == messages[0]["logical_id"]
    await engine.dispose()


def test_canonical_message_delta_keeps_run_causality_without_undo_operation():
    from learning_agent_eval.deltas import build_state_delta
    message = {"logical_id": "chat_message:probe:001", "entity_type": "chat_message",
               "source": "runtime", "scope_ref": "learner:probe", "data": {
                   "run_ref": "agent_run:probe:001", "content": "Confirm minutes."}}
    before = {"capture_status": "complete", "logical_entities": [], "snapshot_sha256": "a" * 64}
    after = {"capture_status": "complete", "logical_entities": [message], "snapshot_sha256": "b" * 64}
    delta = build_state_delta(before, after)
    assert delta["capture_status"] == "complete" and delta["error_codes"] == []
    change = delta["changes"][0]
    assert change["operation_alignment"] == "not_applicable"
    assert change["after"]["value"]["content"] == "Confirm minutes."
    assert {"source_type": "runtime", "ref": "agent_run:probe:001"} in change["source_refs"]
    # An unrelated plan write still needs a matching reversible operation.
    message["entity_type"] = "plan"
    message["logical_id"] = "plan:probe"
    assert build_state_delta(before, after)["error_codes"] == ["delta.operation_unattributed.plan"]


def test_message_effect_requires_the_actual_intervention_invocation_link():
    from learning_agent_eval.exporter_v4 import _invocation_entity_refs
    invocation = {"invocation_id": "tool_invocation:one", "operation_refs": [], "tool_name": "notification.send"}
    after = {"logical_entities": [
        {"logical_id": "chat_message:one", "entity_type": "chat_message", "data": {"run_ref": "run:one"}},
        {"logical_id": "chat_message:unrelated", "entity_type": "chat_message", "data": {"run_ref": "run:one"}},
        {"logical_id": "intervention:one", "entity_type": "intervention", "data": {
            "canonical_message_ref": "chat_message:one", "invocation_ref": "tool_invocation:one"}},
    ]}
    delta = {"changes": [{"entity_ref": row["logical_id"], "operation_refs": []} for row in after["logical_entities"]]}
    assert _invocation_entity_refs(invocation, state_after=after, state_delta=delta, operations={}) == ["chat_message:one", "intervention:one"]
    invocation["invocation_id"] = "tool_invocation:other"
    assert _invocation_entity_refs(invocation, state_after=after, state_delta=delta, operations={}) == []
