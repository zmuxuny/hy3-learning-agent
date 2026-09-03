"""Deterministic OpenAI-compatible stub used by E1 engineering fixtures."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

_PRIVATE_SENTINEL = "E1_PRIVATE_REASONING_SENTINEL_DO_NOT_EXPORT"


class ScriptExhaustedError(RuntimeError):
    pass


class _ToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict[str, Any]):
        self.id = call_id
        self.type = "function"
        self.function = SimpleNamespace(
            name=name,
            arguments=json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


class _Stream:
    def __init__(self, chunks: list[Any]):
        self.chunks = chunks
        self._position = 0

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        if self._position >= len(self.chunks):
            raise StopAsyncIteration
        chunk = self.chunks[self._position]
        self._position += 1
        return chunk


def _usage() -> Any:
    return SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)


class _Completions:
    def __init__(self, turns: list[dict[str, Any]]):
        self._turns = list(turns)
        self._ordinal = 0

    async def create(self, **request: Any) -> Any:
        del request
        if not self._turns:
            raise ScriptExhaustedError("scripted model has no remaining turn")
        self._ordinal += 1
        turn = self._turns.pop(0)
        calls = [
            _ToolCall(item["call_id"], item["name"], item["arguments"])
            for item in turn.get("tool_calls", [])
        ]
        text = str(turn.get("assistant_text") or "")
        if turn.get("delivery") == "nonstream":
            message = SimpleNamespace(
                content=text,
                reasoning_content=_PRIVATE_SENTINEL,
                tool_calls=calls or None,
            )
            return SimpleNamespace(
                id=f"stub-response-{self._ordinal:03d}",
                model="e1-scripted-model",
                choices=[SimpleNamespace(message=message)],
                usage=_usage(),
            )
        delta_calls = [
            SimpleNamespace(index=index, id=call.id, function=call.function)
            for index, call in enumerate(calls)
        ]
        delta = SimpleNamespace(
            content=text,
            reasoning_content=_PRIVATE_SENTINEL,
            tool_calls=delta_calls or None,
        )
        return _Stream(
            [
                SimpleNamespace(
                    id=f"stub-response-{self._ordinal:03d}",
                    model="e1-scripted-model",
                    choices=[SimpleNamespace(delta=delta)],
                    usage=None,
                ),
                SimpleNamespace(
                    id=f"stub-response-{self._ordinal:03d}",
                    model="e1-scripted-model",
                    choices=[],
                    usage=_usage(),
                ),
            ]
        )


class ScriptedModelClient:
    def __init__(self, turns: list[dict[str, Any]]):
        self.chat = SimpleNamespace(completions=_Completions(turns))

    @property
    def remaining_turns(self) -> int:
        return len(self.chat.completions._turns)
