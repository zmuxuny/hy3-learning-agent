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
