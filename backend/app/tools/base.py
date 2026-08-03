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
    session_id: str | None = None
    approval_granted: bool = False


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[dict[str, Any]]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: ToolHandler
    output_model: type[BaseModel] | None = None
    idempotent: bool = False

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
            "idempotent": self.idempotent,
            "input_schema": self.args_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
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
