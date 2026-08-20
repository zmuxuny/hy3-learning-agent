import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import canonical_utc


class EmptyArgs(BaseModel):
    pass


class ToolEffectKind(str, Enum):
    """The persistence/external-effect protocol used by one tool."""

    PURE_READ = "pure_read"
    DATABASE_WRITE = "database_write"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"


@dataclass
class ToolContext:
    db: AsyncSession
    owner_id: str
    run_id: str
    trigger: str
    plan_id: int | None = None
    session_id: str | None = None
    execution_mode: str = "normal"
    reply_to_intervention_id: str | None = None
    approval_granted: bool = False
    # Stable provider-assigned id for one concrete tool call.  A model may
    # intentionally invoke the same write tool twice in one Run; only a replay
    # of this id is deduplicated.  Direct callers without an id get one
    # fail-closed action slot per run/tool and cannot create ambiguous writes.
    tool_call_id: str | None = None
    # Populated by the execution coordinator after the durable claim.  Handlers
    # use these values to associate Operations, evidence, and outbox intents
    # with the canonical invocation instead of inventing parallel identities.
    invocation_id: int | None = None
    action_key: str | None = None
    request_digest: str | None = None
    # Opaque lease ownership for the current executor. Finalization is always
    # fenced by this token so an expired worker cannot overwrite a reclaimed
    # invocation or commit stale domain facts.
    claim_token: str | None = None
    claim_version: int | None = None
    # Runtime opts into an atomic tool.completed row for database writes.  The
    # mutable flag lets the caller avoid emitting a duplicate event afterward.
    persist_completion_event: bool = False
    completion_event_persisted: bool = False
    # Mixed external-read/database-write tools invoke this after all provider
    # waits and before their first ORM mutation/flush. Ordinary write tools are
    # guarded by the registry before the handler starts.
    database_write_ready: Callable[[], Awaitable[None]] | None = None

    async def enter_database_write_phase(self) -> None:
        if self.database_write_ready is not None:
            await self.database_write_ready()


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[dict[str, Any]]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: ToolHandler
    output_model: type[BaseModel] | None = None
    effect_kind: ToolEffectKind = ToolEffectKind.PURE_READ
    idempotent: bool = False
    # True means the tool can produce a blocking approval request.  Runtime
    # guards still decide conditionally whether a concrete invocation blocks.
    blocking: bool = False
    # A mixed-effect tool may perform read-only external preflight before it
    # needs RunEvent sequence serialization. Its handler must explicitly call
    # ``ctx.enter_database_write_phase()`` before any mutation or flush.
    defer_write_guard: bool = False

    def openai_schema(self) -> dict:
        output_fields = []
        if self.output_model is not None:
            output_fields = self.output_model.model_json_schema().get("required", [])
        description = self.description
        if output_fields:
            description += f" Successful output fields: {', '.join(output_fields)}."
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.args_model.model_json_schema(),
            },
        }

    def contract_schema(self) -> dict:
        if self.output_model is None:
            raise RuntimeError(f"Tool {self.name} has no output model")
        return {
            "name": self.name,
            "description": self.description,
            "effect_kind": self.effect_kind.value,
            "idempotent": self.idempotent,
            "blocking": self.blocking,
            "input_schema": self.args_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
        }


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return canonical_utc(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def parse_arguments(raw_arguments: str) -> dict:
    value = json.loads(raw_arguments or "{}")
    if not isinstance(value, dict):
        raise ValueError("Tool arguments must be a JSON object")
    return value
