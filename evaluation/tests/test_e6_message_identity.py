"""Conversation message IDs must never alias notification delivery IDs."""
from types import SimpleNamespace

import pytest

from learning_agent_eval.normalizers import IdentityCandidate, NormalizationError, StableIdentityRegistry
from learning_agent_eval.snapshots import _data, _register_identities, normalize_reference_fields


def registry_with_messages(message_id=2, notification_id=1):
    registry = StableIdentityRegistry("message-identity-probe")
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
    registry = StableIdentityRegistry("probe")
    capture = await collect_state_snapshot(factory, fixture, registry=registry,
        captured_at="2026-09-09T02:00:00Z", resource_version="synthetic-v1", resource_digest="0" * 64, phase="after")
    messages = [e for e in capture.document["logical_entities"] if e["entity_type"] == "chat_message"]
    interventions = [e for e in capture.document["logical_entities"] if e["entity_type"] == "intervention"]
    assert len(messages) == 1 and messages[0]["data"]["content"] == "Please confirm minutes."
    assert interventions[0]["data"]["canonical_message_ref"] == messages[0]["logical_id"]
    assert normalize_reference_fields({"canonical_message_id": 2}, registry)["canonical_message_ref"] == messages[0]["logical_id"]
    await engine.dispose()
