import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession


class EmptyArgs(BaseModel):
    pass


@dataclass
class ToolContext:
    db: AsyncSession
    owner_id: str
    run_id: str
    trigger: str
    plan_id: int | None = None


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[dict[str, Any]]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: ToolHandler

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


def json_safe(value: Any) -> Any:
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
