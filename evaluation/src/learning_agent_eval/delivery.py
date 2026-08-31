"""Zero-side-effect SMTP/Web Push adapter for isolated E1 workers."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .canonical import sha256_digest

SINK_VERSION = "recording-delivery-sink-v1"
_OPAQUE_ROUTING_FIELDS = {
    "reply_token",
    "subscription_id",
    "endpoint",
    "keys",
    "route_digest",
}


class RecordingDeliverySink:
    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        self._identities: dict[str, str] = {}

    def _record(self, action: Any, destination: str) -> dict[str, Any]:
        if destination not in {"smtp", "web_push"}:
            raise ValueError("evaluation sink rejects non-notification destinations")
        payload = dict(action.payload or {})
        # Opaque routing material is deliberately neither recorded nor hashed;
        # stable markers retain the request shape without creating a fingerprint
        # of a recipient, endpoint, push key, or reply credential.
        safe_payload = {
            key: (
                "<opaque-routing-material>"
                if key in _OPAQUE_ROUTING_FIELDS
                else value
            )
            for key, value in payload.items()
        }
        payload_digest = sha256_digest(safe_payload)
        identity = sha256_digest(
            {
                "action_key": action.action_key,
                "destination": destination,
                "request_digest": action.request_digest,
                "payload_digest": payload_digest,
            }
        )
        existing = self._identities.get(action.action_key)
        if existing is not None and existing != identity:
            raise ValueError("evaluation sink action identity conflict")
        if existing is None:
            self._identities[action.action_key] = identity
            self.attempts.append(
                {
                    "ordinal": len(self.attempts) + 1,
                    "action_identity": identity,
                    "action_key": action.action_key,
                    "destination": destination,
                    "request_digest": action.request_digest,
                    "payload_digest": payload_digest,
                    "public_payload_fields": sorted(
                        str(key)
                        for key in action.payload
                        if key not in _OPAQUE_ROUTING_FIELDS
                    ),
                    "opaque_routing_field_count": sum(
                        key in action.payload for key in _OPAQUE_ROUTING_FIELDS
                    ),
                    "sink_version": SINK_VERSION,
                    "external_side_effect": False,
                }
            )
        return {
            "transport": "evaluation_sink",
            "emulated_destination": destination,
            "external_side_effect": False,
            "sink_version": SINK_VERSION,
        }

    async def send_smtp(self, action: Any) -> Any:
        response = self._record(action, "smtp")
        outcome = import_module("app.outbox").DeliveryOutcome
        return outcome(
            status="accepted",
            action_status="delivered",
            provider_id=f"evaluation:{response['sink_version']}",
            response=response,
        )

    async def send_web_push(self, action: Any, *, payload: str) -> Any:
        del payload
        response = self._record(action, "web_push")
        outcome = import_module("app.outbox").DeliveryOutcome
        return outcome(
            status="delivered",
            action_status="delivered",
            provider_id=f"evaluation:{response['sink_version']}",
            response=response,
        )
