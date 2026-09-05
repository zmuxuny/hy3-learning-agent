"""Public-only model request/response recorder for isolated Runtime execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from .canonical import canonical_json, sha256_digest
from .model_budget import INPUT_LIMIT, OUTPUT_LIMIT, ModelBudget
from .normalizers import utc_timestamp
from .privacy import is_model_private_reasoning_field, privacy_issues


class RecorderPrivacyError(RuntimeError):
    """A projected public record failed the existing E0 privacy boundary."""


class RecorderBudgetExceeded(RuntimeError):
    """The shared request budget was exhausted before contacting the provider."""


def public_projection(value: object) -> object:
    """Copy JSON-shaped public fields while never reading private reasoning values."""

    if isinstance(value, Mapping):
        projected: dict[str, object] = {}
        for key in value:
            if is_model_private_reasoning_field(key):
                continue
            projected[str(key)] = public_projection(value[key])
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [public_projection(child) for child in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported recorder projection type: {type(value).__name__}")


def public_tool_arguments(raw_arguments: object) -> tuple[dict[str, Any], str | None]:
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError):
        return {}, "invalid_json"
    if not isinstance(arguments, dict):
        return {}, "non_object_json"
    return public_projection(arguments), None


def _tool_call(call: Any) -> dict[str, Any]:
    function = getattr(call, "function", None)
    raw_arguments = getattr(function, "arguments", "{}") or "{}"
    arguments, error = public_tool_arguments(raw_arguments)
    result = {
        "call_id": str(getattr(call, "id", "")),
        "name": str(getattr(function, "name", "")),
        "canonical_arguments": arguments,
    }
    if error:
        result["argument_error"] = error
    return result


class _RecordedStream:
    def __init__(self, source: AsyncIterator[Any], recorder: EvaluationModelRecorder, pending: dict[str, Any]):
        self._source = source
        self._recorder = recorder
        self._pending = pending

    def __aiter__(self) -> _RecordedStream:
        return self

    @property
    def runtime_model_call_id(self) -> str:
        """Expose the reserved call identity to the generic Runtime seam."""

        return str(self._pending["call_id"])

    async def __anext__(self) -> Any:
        try:
            chunk = await self._source.__anext__()
        except StopAsyncIteration:
            try:
                self._recorder._finish_stream(self._pending)
            except (RecorderPrivacyError, TypeError, ValueError):
                self._recorder._finish_projection_error(self._pending)
                raise
            raise
        except asyncio.CancelledError:
            self._recorder._finish_error(self._pending, status="cancelled")
            raise
        except Exception:
            self._recorder._finish_error(self._pending, status="provider_error")
            raise
        try:
            self._recorder._observe_chunk(self._pending, chunk)
        except (RecorderPrivacyError, AttributeError, IndexError, TypeError, ValueError):
            self._recorder._finish_projection_error(self._pending)
            raise
        return chunk


class _CompletionsDecorator:
    def __init__(self, recorder: EvaluationModelRecorder, delegate: Any):
        self._recorder = recorder
        self._delegate = delegate

    async def create(self, **request: Any) -> Any:
        if self._recorder.max_output_tokens is not None:
            extra_body = request.get("extra_body") or {}
            if any(key in extra_body for key in ("model", "n", "max_tokens", "max_completion_tokens")):
                raise RecorderBudgetExceeded("request body cannot override the request budget")
            if self._recorder.invocation_mode == "real" and request.get("model") != "hy3":
                raise RecorderBudgetExceeded("request budget is priced for Hy3 only")
            request["max_tokens"] = self._recorder.max_output_tokens
            request.pop("max_completion_tokens", None)
            request["n"] = 1
            if self._recorder.budget is not None and request.get("stream"):
                request["stream_options"] = {"include_usage": True}
        if self._recorder.budget is not None:
            # Includes tools, all replayed messages and protocol overhead. This
            # bound is local admission control, not measured provider token use.
            from .canonical import canonical_json_bytes

            if len(canonical_json_bytes(request)) + 2048 > INPUT_LIMIT:
                raise RecorderBudgetExceeded("shared priced input envelope exceeded")
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
        try:
            self._recorder._finish_message(pending, response)
        except (RecorderPrivacyError, AttributeError, IndexError, TypeError, ValueError):
            self._recorder._finish_projection_error(pending)
            raise
        # Preserve the provider SDK's response identity. Runtime consumers may
        # rely on it, and OpenAI-compatible response models have a writable
        # instance dictionary even when their public schema forbids extras.
        call_id = str(pending["call_id"])
        try:
            object.__setattr__(response, "runtime_model_call_id", call_id)
        except (AttributeError, TypeError, ValueError):
            message = response.choices[0].message
            object.__setattr__(message, "runtime_model_call_id", call_id)
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
        max_calls: int | None = None,
        max_output_tokens: int | None = None,
        budget: ModelBudget | None = None,
        budget_scope: str = "unspecified",
    ):
        if invocation_mode not in {"stub", "real"}:
            raise ValueError("invocation_mode must be stub or real")
        self.chat = _ChatDecorator(self, client.chat)
        self.invocation_mode = invocation_mode
        self._metadata_provider = metadata_provider
        self.records: list[dict[str, Any]] = []
        self._record_lock = Lock()
        self._next_ordinal = 1
        self.max_calls = max_calls
        self.max_output_tokens = max_output_tokens
        self.budget = budget
        self.budget_scope = budget_scope
        if budget is not None and (type(max_output_tokens) is not int or not 1 <= max_output_tokens <= OUTPUT_LIMIT):
            raise ValueError("prepaid budget requires a priced output limit")
        if max_calls is not None and (type(max_calls) is not int or max_calls < 1):
            raise ValueError("max_calls must be a positive integer")
        if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens < 1):
            raise ValueError("max_output_tokens must be a positive integer")

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
        # Reserve identity at call start. Provider calls can complete out of
        # order, so completion-order list length is not a valid allocator.
        with self._record_lock:
            if self.max_calls is not None and self._next_ordinal > self.max_calls:
                raise RecorderBudgetExceeded("shared model request budget exhausted")
            ordinal = self._next_ordinal
            self._next_ordinal += 1
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
                "n": request.get("n", 1),
            },
            "token_usage": None,
            "response_model": None,
            "provider_request_id": None,
            "response_status": "pending",
            "response_digest": None,
            "invocation_mode": self.invocation_mode,
            "_stream_calls": {},
        }
        if self.invocation_mode == "real":
            pending["requested_at"] = utc_timestamp(datetime.now(timezone.utc))
            pending["responded_at"] = None
        self._assert_public(pending)
        if self.budget is not None:
            pending["_budget_ticket"] = self.budget.reserve(
                scope=self.budget_scope, call_id=pending["call_id"],
                output_limit=self.max_output_tokens,
            )
        return pending

    def _observe_chunk(self, pending: dict[str, Any], chunk: Any) -> None:
        self._observe_usage(pending, chunk)
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
            arguments, error = public_tool_arguments(raw["arguments"] or "{}")
            calls.append(
                {
                    "call_id": raw["call_id"],
                    "name": raw["name"],
                    "canonical_arguments": arguments,
                    **({"argument_error": error} if error else {}),
                }
            )
        pending["function_calls"] = calls
        pending["response_status"] = "completed"
        if self.invocation_mode == "real":
            pending["responded_at"] = utc_timestamp(datetime.now(timezone.utc))
        pending["response_digest"] = sha256_digest(
            {
                "assistant_text": pending["assistant_text"],
                "function_calls": pending["function_calls"],
            }
        )
        self._publish(pending)

    def _finish_message(self, pending: dict[str, Any], response: Any) -> None:
        self._observe_usage(pending, response)
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
        if self.invocation_mode == "real":
            pending["responded_at"] = utc_timestamp(datetime.now(timezone.utc))
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
        if self.invocation_mode == "real":
            pending["responded_at"] = utc_timestamp(datetime.now(timezone.utc))
        pending["response_digest"] = None
        self._publish(pending)

    def _finish_projection_error(self, pending: dict[str, Any]) -> None:
        # Preserve the public request and attempt without publishing unsafe or
        # unparseable response material. Never include an exception's text.
        pending["assistant_text"] = ""
        pending["function_calls"] = []
        pending["record_error"] = "response_projection_rejected"
        self._finish_error(pending, status="framework_error")

    @staticmethod
    def _observe_usage(pending: dict[str, Any], response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        values = {name: getattr(usage, name, None) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
        if all(type(value) is int and value >= 0 for value in values.values()):
            pending["token_usage"] = values

    def _publish(self, record: dict[str, Any]) -> None:
        ticket = record.pop("_budget_ticket", None)
        if ticket is not None:
            self.budget.settle(ticket, record.get("token_usage"))
        self._assert_public(record)
        # Canonical round-trip prevents later mutation through caller-owned containers.
        published = json.loads(canonical_json(record))
        with self._record_lock:
            self.records.append(published)
            self.records.sort(key=lambda item: int(item["ordinal"]))

    @staticmethod
    def _assert_public(value: object) -> None:
        if privacy_issues(value, file="<model-record>"):
            raise RecorderPrivacyError("model record violates the public projection")
