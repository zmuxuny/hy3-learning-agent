"""Lossless deduplication of repeated public evidence in the Judge wire request."""

from collections import Counter

from .canonical import canonical_json
from .validator import resolve_evidence_path


def pack_shared_values(document):
    counts = Counter()

    def count(value):
        encoded = canonical_json(value)
        if len(encoded.encode("utf-8")) >= 256:
            counts[encoded] += 1
        if isinstance(value, dict):
            for child in value.values():
                count(child)
        elif isinstance(value, list):
            for child in value:
                count(child)

    count(document)
    names, shared = {}, {}

    def children(value):
        if isinstance(value, dict):
            if set(value) in ({"$shared"}, {"$literal"}):
                return {"$literal": [[key, pack(child)] for key, child in value.items()]}
            return {key: pack(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [pack(child) for child in value]
        return value

    def pack(value):
        encoded = canonical_json(value)
        if counts[encoded] > 1:
            if encoded not in names:
                name = f"v{len(names) + 1}"
                names[encoded] = name
                shared[name] = children(value)
            return {"$shared": names[encoded]}
        return children(value)

    packed = pack(document)
    return {"encoding": "shared-json-v1", "shared_values": shared, "document": packed}


def unpack_shared_values(packed):
    """For independent verification: reconstruct the exact blind input."""
    def expand(value):
        if isinstance(value, dict):
            if set(value) == {"$shared"}:
                return expand(packed["shared_values"][value["$shared"]])
            if set(value) == {"$literal"}:
                return {key: expand(child) for key, child in value["$literal"]}
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value
    return expand(packed["document"])


def evidence_catalog(episode):
    """Offer exact, short Episode paths; never expose blinded control metadata."""
    paths = ["trigger.objective", "result.action_class", "result.user_visible_output",
             "result.layers", "state_after", "state_delta", "environment.policies"]
    trace = episode["observable_trace"]
    for i, _ in enumerate(trace["model_calls"]):
        paths.extend([f"observable_trace.model_calls[{i}].assistant_text",
                      f"observable_trace.model_calls[{i}].returned_tool_calls",
                      f"observable_trace.model_calls[{i}].visible_context.messages"])
    for i, _ in enumerate(trace["tool_invocations"]):
        paths.extend([f"observable_trace.tool_invocations[{i}].canonical_args",
                      f"observable_trace.tool_invocations[{i}].result"])
    paths.extend(f"observable_trace.guard_decisions[{i}]" for i, _ in enumerate(trace["guard_decisions"]))
    return [path for path in paths if resolve_evidence_path(episode, path)[0]]


def draft_reading_aid(episode):
    """Expose complete draft objects for reading, without removing source evidence.

    Product tool arguments may encode a plan as JSON text. Decode only that
    transport for this redundant view and cite its original, visible path.
    """
    import json

    drafts = []
    for index, call in enumerate(episode["observable_trace"]["model_calls"]):
        for tool in call.get("returned_tool_calls", []):
            plan = tool.get("canonical_arguments", {}).get("plan")
            if isinstance(plan, str):
                try:
                    plan = json.loads(plan)
                except ValueError:
                    continue
            if isinstance(plan, dict):
                drafts.append({"evidence_path": f"observable_trace.model_calls[{index}].returned_tool_calls",
                               "draft_plan": plan})
    return drafts
