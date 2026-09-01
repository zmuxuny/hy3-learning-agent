"""Public-only model request/response recorder for isolated Runtime execution."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .canonical import canonical_json, sha256_digest
from .privacy import is_private_reasoning_field, privacy_issues


class RecorderPrivacyError(RuntimeError):
    """A projected public record failed the existing E0 privacy boundary."""


_SYSTEM_MESSAGE_MARKER = "[system prompt body omitted; see version and digest]"


def public_projection(value: object) -> object:
    """Copy JSON-shaped public fields while never reading private reasoning values."""

    if isinstance(value, Mapping):
        projected: dict[str, object] = {}
        for key in value:
            if is_private_reasoning_field(key):
                continue
            projected[str(key)] = public_projection(value[key])
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [public_projection(child) for child in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported recorder projection type: {type(value).__name__}")


def _tool_call(call: Any) -> dict[str, Any]:
    function = getattr(call, "function", None)
    raw_arguments = getattr(function, "arguments", "{}") or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RecorderPrivacyError("model tool arguments are not canonical JSON") from exc
    if not isinstance(arguments, dict):
        raise RecorderPrivacyError("model tool arguments must be an object")
    return {
        "call_id": str(getattr(call, "id", "")),
        "name": str(getattr(function, "name", "")),
        "canonical_arguments": public_projection(arguments),
    }


class _RecordedStream:
    def __init__(self, source: AsyncIterator[Any], recorder: EvaluationModelRecorder, pending: dict[str, Any]):
        self._source = source
        self._recorder = recorder
        self._pending = pending

    def __aiter__(self) -> _RecordedStream:
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._source.__anext__()
        except StopAsyncIteration:
            self._recorder._finish_stream(self._pending)
            raise
        self._recorder._observe_chunk(self._pending, chunk)
        return chunk


class _CompletionsDecorator:
    def __init__(self, recorder: EvaluationModelRecorder, delegate: Any):
        self._recorder = recorder
        self._delegate = delegate

    async def create(self, **request: Any) -> Any:
        pending = self._recorder._begin(request)
        response = await self._delegate.create(**request)
        if hasattr(response, "__aiter__"):
            return _RecordedStream(response.__aiter__(), self._recorder, pending)
        self._recorder._finish_message(pending, response.choices[0].message)
        return response


class _ChatDecorator:
    def __init__(self, recorder: EvaluationModelRecorder, delegate: Any):
        self.completions = _CompletionsDecorator(recorder, delegate.completions)


class EvaluationModelRecorder:
    """OpenAI-compatible client decorator with a deliberately narrow projection."""

    def __init__(self, client: Any, *, invocation_mode: str):
        if invocation_mode not in {"stub", "real"}:
            raise ValueError("invocation_mode must be stub or real")
        self.chat = _ChatDecorator(self, client.chat)
        self.invocation_mode = invocation_mode
        self.records: list[dict[str, Any]] = []

    def _begin(self, request: Mapping[str, Any]) -> dict[str, Any]:
        projected_messages = public_projection(request.get("messages") or [])
        tools = public_projection(request.get("tools") or [])
        system_text = ""
        visible_messages: list[object] = []
        if isinstance(projected_messages, list):
            for message in projected_messages:
                if isinstance(message, dict) and message.get("role") == "system":
                    system_text = str(message.get("content") or "")
                    visible_messages.append(
                        {"role": "system", "content": _SYSTEM_MESSAGE_MARKER}
                    )
                    continue
                visible_messages.append(message)
        allowlist = [
            str(tool.get("function", {}).get("name", ""))
            for tool in tools
            if isinstance(tool, dict)
        ] if isinstance(tools, list) else []
        pending = {
            "ordinal": len(self.records) + 1,
            "visible_messages": visible_messages,
            "visible_input_digest": sha256_digest(visible_messages),
            "system_prompt": {
                "version": "agent-system-prompt-v1",
                "digest": sha256_digest(system_text),
            },
            "tools": {
                "allowlist": allowlist,
                "schema_digest": sha256_digest(tools),
            },
            "assistant_text": "",
            "function_calls": [],
            "invocation_mode": self.invocation_mode,
            "_stream_calls": {},
        }
        self._assert_public(pending)
        return pending

    def _observe_chunk(self, pending: dict[str, Any], chunk: Any) -> None:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        delta = getattr(choices[0], "delta", None) or getattr(choices[0], "message", None)
        if delta is None:
            return
        content = getattr(delta, "content", None)
        if content:
            pending["assistant_text"] += str(content)
        for call in getattr(delta, "tool_calls", None) or []:
            index = int(getattr(call, "index", 0))
            accumulated = pending["_stream_calls"].setdefault(
                index,
                {"call_id": "", "name": "", "arguments": ""},
            )
            if getattr(call, "id", None):
                accumulated["call_id"] = str(call.id)
            function = getattr(call, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    accumulated["name"] += str(function.name)
                if getattr(function, "arguments", None):
                    accumulated["arguments"] += str(function.arguments)

    def _finish_stream(self, pending: dict[str, Any]) -> None:
        calls = []
        for _, raw in sorted(pending.pop("_stream_calls").items()):
            try:
                arguments = json.loads(raw["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                raise RecorderPrivacyError("model tool arguments are not canonical JSON") from exc
            if not isinstance(arguments, dict):
                raise RecorderPrivacyError("model tool arguments must be an object")
            calls.append(
                {
                    "call_id": raw["call_id"],
                    "name": raw["name"],
                    "canonical_arguments": public_projection(arguments),
                }
            )
        pending["function_calls"] = calls
        self._publish(pending)

    def _finish_message(self, pending: dict[str, Any], message: Any) -> None:
        pending.pop("_stream_calls")
        pending["assistant_text"] = str(getattr(message, "content", None) or "")
        pending["function_calls"] = [
            _tool_call(call) for call in (getattr(message, "tool_calls", None) or [])
        ]
        self._publish(pending)

    def _publish(self, record: dict[str, Any]) -> None:
        self._assert_public(record)
        # Canonical round-trip prevents later mutation through caller-owned containers.
        self.records.append(json.loads(canonical_json(record)))

    @staticmethod
    def _assert_public(value: object) -> None:
        if privacy_issues(value, file="<model-record>"):
            raise RecorderPrivacyError("model record violates the public projection")
