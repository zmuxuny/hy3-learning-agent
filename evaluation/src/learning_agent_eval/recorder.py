"""Public-only model request/response recorder for isolated Runtime execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from .canonical import canonical_json, sha256_digest
from .privacy import is_private_reasoning_field, privacy_issues


class RecorderPrivacyError(RuntimeError):
    """A projected public record failed the existing E0 privacy boundary."""


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
        except asyncio.CancelledError:
            self._recorder._finish_error(self._pending, status="cancelled")
            raise
        except Exception:
            self._recorder._finish_error(self._pending, status="provider_error")
            raise
        self._recorder._observe_chunk(self._pending, chunk)
        return chunk


class _CompletionsDecorator:
    def __init__(self, recorder: EvaluationModelRecorder, delegate: Any):
        self._recorder = recorder
        self._delegate = delegate

    async def create(self, **request: Any) -> Any:
        pending = self._recorder._begin(request)
        try:
            response = await self._delegate.create(**request)
        except asyncio.CancelledError:
            self._recorder._finish_error(pending, status="cancelled")
            raise
        except Exception:
            self._recorder._finish_error(pending, status="provider_error")
            raise
        if hasattr(response, "__aiter__"):
            return _RecordedStream(response.__aiter__(), self._recorder, pending)
        self._recorder._finish_message(pending, response)
        return response


class _ChatDecorator:
    def __init__(self, recorder: EvaluationModelRecorder, delegate: Any):
        self.completions = _CompletionsDecorator(recorder, delegate.completions)


class EvaluationModelRecorder:
    """OpenAI-compatible client decorator with a deliberately narrow projection."""

    def __init__(
        self,
        client: Any,
        *,
        invocation_mode: str,
        metadata_provider: Callable[[], object | None] | None = None,
    ):
        if invocation_mode not in {"stub", "real"}:
            raise ValueError("invocation_mode must be stub or real")
        self.chat = _ChatDecorator(self, client.chat)
        self.invocation_mode = invocation_mode
        self._metadata_provider = metadata_provider
        self.records: list[dict[str, Any]] = []

    def _begin(self, request: Mapping[str, Any]) -> dict[str, Any]:
        projected_messages = public_projection(request.get("messages") or [])
        tools = public_projection(request.get("tools") or [])
        system_text = ""
        if isinstance(projected_messages, list):
            for message in projected_messages:
                if isinstance(message, dict) and message.get("role") == "system":
                    system_text = str(message.get("content") or "")
        visible_messages = projected_messages if isinstance(projected_messages, list) else []
        allowlist = [
            str(tool.get("function", {}).get("name", ""))
            for tool in tools
            if isinstance(tool, dict)
        ] if isinstance(tools, list) else []
        ordinal = len(self.records) + 1
        metadata = self._metadata_provider() if self._metadata_provider else None
        pending = {
            "call_id": f"model-call:{ordinal:03d}",
            "ordinal": ordinal,
            "run_id": str(getattr(metadata, "run_id", "unscoped-run")),
            "parent_run_id": getattr(metadata, "parent_run_id", None),
            "parent_call_id": getattr(metadata, "parent_call_id", None),
            "depth": int(getattr(metadata, "depth", 0)),
            "call_purpose": str(getattr(metadata, "call_purpose", "decision")),
            "decision_relevant": bool(
                getattr(metadata, "decision_relevant", True)
            ),
            "visible_messages": visible_messages,
            "visible_tool_schemas": tools,
            "visible_context_digest": sha256_digest(
                {"messages": visible_messages, "tool_schemas": tools}
            ),
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
            "request_model": str(request.get("model") or "unspecified-model"),
            "request_config": {
                "temperature": request.get("temperature"),
                "max_tokens": request.get("max_tokens"),
                "effort_setting": (
                    request.get("extra_body", {}).get("reasoning_effort")
                    if isinstance(request.get("extra_body"), Mapping)
                    else None
                ),
                "stream": bool(request.get("stream", False)),
            },
            "response_model": None,
            "provider_request_id": None,
            "response_status": "pending",
            "response_digest": None,
            "invocation_mode": self.invocation_mode,
            "_stream_calls": {},
        }
        self._assert_public(pending)
        return pending

    def _observe_chunk(self, pending: dict[str, Any], chunk: Any) -> None:
        if getattr(chunk, "model", None):
            pending["response_model"] = str(chunk.model)
        if getattr(chunk, "id", None):
            pending["provider_request_id"] = str(chunk.id)
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
        pending["response_status"] = "completed"
        pending["response_digest"] = sha256_digest(
            {
                "assistant_text": pending["assistant_text"],
                "function_calls": pending["function_calls"],
            }
        )
        self._publish(pending)

    def _finish_message(self, pending: dict[str, Any], response: Any) -> None:
        pending.pop("_stream_calls")
        message = response.choices[0].message
        pending["assistant_text"] = str(getattr(message, "content", None) or "")
        pending["function_calls"] = [
            _tool_call(call) for call in (getattr(message, "tool_calls", None) or [])
        ]
        if getattr(response, "model", None):
            pending["response_model"] = str(response.model)
        if getattr(response, "id", None):
            pending["provider_request_id"] = str(response.id)
        pending["response_status"] = "completed"
        pending["response_digest"] = sha256_digest(
            {
                "assistant_text": pending["assistant_text"],
                "function_calls": pending["function_calls"],
            }
        )
        self._publish(pending)

    def _finish_error(self, pending: dict[str, Any], *, status: str) -> None:
        pending.pop("_stream_calls", None)
        pending["response_status"] = status
        pending["response_digest"] = None
        self._publish(pending)

    def _publish(self, record: dict[str, Any]) -> None:
        self._assert_public(record)
        # Canonical round-trip prevents later mutation through caller-owned containers.
        self.records.append(json.loads(canonical_json(record)))

    @staticmethod
    def _assert_public(value: object) -> None:
        if privacy_issues(value, file="<model-record>"):
            raise RecorderPrivacyError("model record violates the public projection")
