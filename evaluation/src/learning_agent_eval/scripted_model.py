"""Deterministic OpenAI-compatible stub used by E1 engineering fixtures."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from .action_protocol import render_action_declaration

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
        self._fallback_response_ordinal = 0

    @staticmethod
    def _user_contents(request: dict[str, Any]) -> tuple[str, ...]:
        messages = request.get("messages")
        if not isinstance(messages, list):
            return ()
        return tuple(
            str(message.get("content") or "")
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        )

    def _select_turn(self, request: dict[str, Any]) -> dict[str, Any]:
        """Route concurrent fixture calls by explicit public request identity.

        Selectors exist only in fixed engineering fixtures. They do not infer
        behavior or scores from model text; they prevent concurrently scheduled
        child calls from consuming one another's predetermined response.
        """

        user_contents = self._user_contents(request)
        matching = [
            (index, turn)
            for index, turn in enumerate(self._turns)
            if (
                (prefix := turn.get("stub_request_user_prefix")) is not None
                and any(content.startswith(str(prefix)) for content in user_contents)
            )
        ]
        if len(matching) > 1:
            raise ScriptExhaustedError("scripted request matches multiple turns")
        if matching:
            index, _ = matching[0]
            return self._turns.pop(index)
        for index, turn in enumerate(self._turns):
            if turn.get("stub_request_user_prefix") is None:
                return self._turns.pop(index)
        raise ScriptExhaustedError("scripted request has no matching turn")

    async def create(self, **request: Any) -> Any:
        if not self._turns:
            raise ScriptExhaustedError("scripted model has no remaining turn")
        turn = self._select_turn(request)
        if turn.get("ordinal") is None:
            self._fallback_response_ordinal += 1
            response_ordinal = self._fallback_response_ordinal
        else:
            response_ordinal = int(turn["ordinal"])
        calls = [
            _ToolCall(item["call_id"], item["name"], item["arguments"])
            for item in turn.get("tool_calls", [])
        ]
        text = render_action_declaration(
            str(turn.get("assistant_text") or ""),
            turn.get("declared_actions") or [],
        )
        if turn.get("delivery") == "nonstream":
            message = SimpleNamespace(
                content=text,
                reasoning_content=_PRIVATE_SENTINEL,
                tool_calls=calls or None,
            )
            return SimpleNamespace(
                id=f"stub-response-{response_ordinal:03d}",
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
                    id=f"stub-response-{response_ordinal:03d}",
                    model="e1-scripted-model",
                    choices=[SimpleNamespace(delta=delta)],
                    usage=None,
                ),
                SimpleNamespace(
                    id=f"stub-response-{response_ordinal:03d}",
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
