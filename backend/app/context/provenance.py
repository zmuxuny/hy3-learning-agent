from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from enum import Enum
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import canonical_utc, coerce_legacy_utc, utc_now
from app.db.uow import flush as flush_uow
from app.models import (
    ContextState,
    Memory,
    MemoryLifecycleEvent,
    ProvenanceEdge,
    ProvenanceNode,
    Session,
)


_UNSET = object()


class ProvenanceConflict(RuntimeError):
    """An idempotency identity was reused with different immutable content."""


class MemoryLifecycleConflict(RuntimeError):
    """A Memory lifecycle action lost its version fence or changed its request."""


def _transition_argument(value: Any) -> Any:
    return {"$state": "unset"} if value is _UNSET else value


def memory_transition_digest(
    *,
    owner_id: str,
    memory_id: int,
    expected_version: int,
    event_type: str,
    action_key: str,
    to_status: str | None,
    to_validity_state: str | None,
    reason_code: str,
    expires_at: datetime | None | object,
    archived_from_status: str | None | object,
    archived_reason: str | object,
    confidence: float | object,
    source_run_id: str | None,
    source_message_id: int | None,
    additional_source_node_id: str | None,
) -> str:
    """Canonical identity for every semantic argument of a Memory transition."""

    return canonical_digest(
        {
            "protocol": "h5-memory-transition-v1",
            "owner_id": owner_id,
            "memory_id": memory_id,
            "expected_version": expected_version,
            "event_type": event_type,
            "action_key": action_key,
            "to_status": to_status,
            "to_validity_state": to_validity_state,
            "reason_code": reason_code,
            "expires_at": _transition_argument(expires_at),
            "archived_from_status": _transition_argument(archived_from_status),
            "archived_reason": _transition_argument(archived_reason),
            "confidence": _transition_argument(confidence),
            "source_run_id": source_run_id,
            "source_message_id": source_message_id,
            "additional_source_node_id": additional_source_node_id,
        }
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return canonical_utc(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, bytes):
        return {"$bytes_sha256": hashlib.sha256(value).hexdigest(), "$bytes_size": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical provenance value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return the stable UTF-8 JSON representation used by H5 identities."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    """Hash text/bytes exactly and structured values as canonical JSON."""

    if isinstance(value, str):
        payload = value.encode("utf-8")
    elif isinstance(value, bytes):
        payload = value
    else:
        payload = canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def ensure_provenance_node(
    db: AsyncSession,
    *,
    owner_id: str,
    kind: str,
    entity_key: str | int,
    entity_version: int,
    content_digest: str,
    plan_id: int | None = None,
    session_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProvenanceNode:
    """Idempotently materialize one immutable source-graph node.

    The unique entity/version identity is stable across retries. Reusing it with
    another digest, scope or metadata is a conflict rather than a silent rewrite.
    """

    if entity_version < 1:
        raise ValueError("provenance entity_version must be positive")
    if (
        len(content_digest) != 64
        or content_digest != content_digest.lower()
        or any(character not in "0123456789abcdef" for character in content_digest)
    ):
        raise ValueError("provenance content_digest must be a SHA-256 hex digest")
    entity_key_text = str(entity_key)
    node_metadata = dict(_json_value(dict(metadata or {})))
    await db.execute(
        sqlite_insert(ProvenanceNode.__table__)
        .values(
            owner_id=owner_id,
            plan_id=plan_id,
            session_id=session_id,
            kind=kind,
            entity_key=entity_key_text,
            entity_version=entity_version,
            content_digest=content_digest,
            metadata=node_metadata,
        )
        .on_conflict_do_nothing(
            index_elements=["owner_id", "kind", "entity_key", "entity_version"]
        )
    )
    node = (
        await db.execute(
            select(ProvenanceNode).where(
                ProvenanceNode.owner_id == owner_id,
                ProvenanceNode.kind == kind,
                ProvenanceNode.entity_key == entity_key_text,
                ProvenanceNode.entity_version == entity_version,
            )
        )
    ).scalar_one()
    expected = (plan_id, session_id, content_digest, node_metadata)
    actual = (node.plan_id, node.session_id, node.content_digest, node.node_metadata)
    if actual != expected:
        raise ProvenanceConflict(
            f"provenance node identity conflict: {kind}:{entity_key_text}:v{entity_version}"
        )
    return node


async def ensure_provenance_nodes(
    db: AsyncSession,
    nodes: Sequence[Mapping[str, Any]],
) -> list[ProvenanceNode]:
    """Batch variant of :func:`ensure_provenance_node` for large source ranges."""

    result: list[ProvenanceNode] = []
    for start in range(0, len(nodes), 200):
        batch = nodes[start : start + 200]
        if not batch:
            continue
        values: list[dict[str, Any]] = []
        for raw in batch:
            entity_version = int(raw["entity_version"])
            content_digest = str(raw["content_digest"])
            if (
                entity_version < 1
                or len(content_digest) != 64
                or content_digest != content_digest.lower()
                or any(
                    character not in "0123456789abcdef"
                    for character in content_digest
                )
            ):
                raise ValueError("invalid batched provenance node identity")
            values.append(
                {
                    "owner_id": str(raw["owner_id"]),
                    "plan_id": raw.get("plan_id"),
                    "session_id": raw.get("session_id"),
                    "kind": str(raw["kind"]),
                    "entity_key": str(raw["entity_key"]),
                    "entity_version": entity_version,
                    "content_digest": content_digest,
                    "metadata": dict(_json_value(dict(raw.get("metadata") or {}))),
                }
            )
        await db.execute(
            sqlite_insert(ProvenanceNode.__table__).values(values).on_conflict_do_nothing()
        )
        owner_ids = sorted({str(item["owner_id"]) for item in values})
        kinds = sorted({str(item["kind"]) for item in values})
        entity_keys = sorted({str(item["entity_key"]) for item in values})
        entity_versions = sorted({int(item["entity_version"]) for item in values})
        stored = list(
            (
                await db.execute(
                    select(ProvenanceNode).where(
                        ProvenanceNode.owner_id.in_(owner_ids),
                        ProvenanceNode.kind.in_(kinds),
                        ProvenanceNode.entity_key.in_(entity_keys),
                        ProvenanceNode.entity_version.in_(entity_versions),
                    )
                )
            ).scalars()
        )
        by_identity = {
            (item.owner_id, item.kind, item.entity_key, item.entity_version): item
            for item in stored
        }
        for expected in values:
            node = by_identity.get(
                (
                    expected["owner_id"],
                    expected["kind"],
                    expected["entity_key"],
                    expected["entity_version"],
                )
            )
            if node is None:
                raise ProvenanceConflict("batched provenance node could not be materialized")
            if (
                node.plan_id,
                node.session_id,
                node.content_digest,
                node.node_metadata,
            ) != (
                expected["plan_id"],
                expected["session_id"],
                expected["content_digest"],
                expected["metadata"],
            ):
                raise ProvenanceConflict(
                    "batched provenance node identity conflict: "
                    f"{expected['kind']}:{expected['entity_key']}:v{expected['entity_version']}"
                )
            result.append(node)
    return result


async def append_provenance_edge(
    db: AsyncSession,
    *,
    owner_id: str,
    source_node_id: str,
    target_node_id: str,
    relation: str,
    ordinal: int,
    disposition: str = "none",
    reason_code: str = "",
    source_bytes: int | None = None,
    read_bytes: int | None = None,
    chunk_count: int | None = None,
    token_count: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProvenanceEdge:
    """Append one immutable edge, accepting only an exact idempotent replay."""

    if ordinal < 0:
        raise ValueError("provenance edge ordinal cannot be negative")
    edge_metadata = dict(_json_value(dict(metadata or {})))
    values = {
        "owner_id": owner_id,
        "source_node_id": source_node_id,
        "target_node_id": target_node_id,
        "relation": relation,
        "ordinal": ordinal,
        "disposition": disposition,
        "reason_code": reason_code,
        "source_bytes": source_bytes,
        "read_bytes": read_bytes,
        "chunk_count": chunk_count,
        "token_count": token_count,
        "metadata": edge_metadata,
    }
    await db.execute(
        sqlite_insert(ProvenanceEdge.__table__).values(**values).on_conflict_do_nothing()
    )
    edge = (
        await db.execute(
            select(ProvenanceEdge).where(
                ProvenanceEdge.target_node_id == target_node_id,
                ProvenanceEdge.relation == relation,
                ProvenanceEdge.ordinal == ordinal,
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        edge = (
            await db.execute(
                select(ProvenanceEdge).where(
                    ProvenanceEdge.source_node_id == source_node_id,
                    ProvenanceEdge.target_node_id == target_node_id,
                    ProvenanceEdge.relation == relation,
                )
            )
        ).scalar_one_or_none()
    if edge is None:
        raise ProvenanceConflict("provenance edge could not be materialized")
    expected = (
        owner_id,
        source_node_id,
        target_node_id,
        relation,
        ordinal,
        disposition,
        reason_code,
        source_bytes,
        read_bytes,
        chunk_count,
        token_count,
        edge_metadata,
    )
    actual = (
        edge.owner_id,
        edge.source_node_id,
        edge.target_node_id,
        edge.relation,
        edge.ordinal,
        edge.disposition,
        edge.reason_code,
        edge.source_bytes,
        edge.read_bytes,
        edge.chunk_count,
        edge.token_count,
        edge.edge_metadata,
    )
    if actual != expected:
        raise ProvenanceConflict(
            f"provenance edge identity conflict: {target_node_id}:{relation}:{ordinal}"
        )
    return edge


async def append_provenance_edges(
    db: AsyncSession,
    edges: Sequence[Mapping[str, Any]],
) -> list[ProvenanceEdge]:
    """Batch append immutable edges while retaining exact replay checks."""

    result: list[ProvenanceEdge] = []
    for start in range(0, len(edges), 100):
        batch = edges[start : start + 100]
        values: list[dict[str, Any]] = []
        for raw in batch:
            ordinal = int(raw["ordinal"])
            if ordinal < 0:
                raise ValueError("provenance edge ordinal cannot be negative")
            values.append(
                {
                    "owner_id": str(raw["owner_id"]),
                    "source_node_id": str(raw["source_node_id"]),
                    "target_node_id": str(raw["target_node_id"]),
                    "relation": str(raw["relation"]),
                    "ordinal": ordinal,
                    "disposition": str(raw.get("disposition", "none")),
                    "reason_code": str(raw.get("reason_code", "")),
                    "source_bytes": raw.get("source_bytes"),
                    "read_bytes": raw.get("read_bytes"),
                    "chunk_count": raw.get("chunk_count"),
                    "token_count": raw.get("token_count"),
                    "metadata": dict(_json_value(dict(raw.get("metadata") or {}))),
                }
            )
        if not values:
            continue
        await db.execute(
            sqlite_insert(ProvenanceEdge.__table__).values(values).on_conflict_do_nothing()
        )
        target_ids = sorted({str(item["target_node_id"]) for item in values})
        relations = sorted({str(item["relation"]) for item in values})
        stored = list(
            (
                await db.execute(
                    select(ProvenanceEdge).where(
                        ProvenanceEdge.target_node_id.in_(target_ids),
                        ProvenanceEdge.relation.in_(relations),
                    )
                )
            ).scalars()
        )
        by_ordinal = {
            (item.target_node_id, item.relation, item.ordinal): item for item in stored
        }
        for expected in values:
            key = (
                expected["target_node_id"],
                expected["relation"],
                expected["ordinal"],
            )
            edge = by_ordinal.get(key)
            if edge is None:
                raise ProvenanceConflict("batched provenance edge could not be materialized")
            actual = {
                "owner_id": edge.owner_id,
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "relation": edge.relation,
                "ordinal": edge.ordinal,
                "disposition": edge.disposition,
                "reason_code": edge.reason_code,
                "source_bytes": edge.source_bytes,
                "read_bytes": edge.read_bytes,
                "chunk_count": edge.chunk_count,
                "token_count": edge.token_count,
                "metadata": edge.edge_metadata,
            }
            if actual != expected:
                raise ProvenanceConflict(
                    "batched provenance edge identity conflict: "
                    f"{expected['target_node_id']}:{expected['relation']}:{expected['ordinal']}"
                )
            result.append(edge)
    return result


async def read_context_generation(db: AsyncSession, owner_id: str) -> int:
    # Owners are created after the canonical schema, so migration-time seeding
    # cannot guarantee this row exists. Every Context reader establishes the
    # zero-generation fence before it returns a value that a finalize trigger
    # will later require to be durable.
    await db.execute(
        sqlite_insert(ContextState)
        .values(owner_id=owner_id, generation=0, updated_at=utc_now())
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    generation = await db.scalar(
        select(ContextState.generation).where(ContextState.owner_id == owner_id)
    )
    if generation is None:
        raise RuntimeError("context generation row could not be established")
    return int(generation)


async def bump_context_generation(db: AsyncSession, owner_id: str) -> int:
    """Atomically advance the owner Context generation inside the caller UoW."""

    await db.execute(
        sqlite_insert(ContextState)
        .values(owner_id=owner_id, generation=0, updated_at=utc_now())
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    generation = await db.scalar(
        update(ContextState)
        .where(ContextState.owner_id == owner_id)
        .values(generation=ContextState.generation + 1, updated_at=utc_now())
        .returning(ContextState.generation)
    )
    if generation is None:
        raise RuntimeError("context generation row disappeared during bump")
    return int(generation)


async def provenance_descendant_node_ids(
    db: AsyncSession,
    *,
    owner_id: str,
    source_node_ids: Sequence[str],
) -> set[str]:
    """Return the owner-scoped transitive closure, including every seed node."""

    seeds = sorted({str(node_id) for node_id in source_node_ids if node_id})
    if not seeds:
        return set()
    bind_names = ", ".join(f":seed_{index}" for index in range(len(seeds)))
    params = {"owner_id": owner_id, **{f"seed_{index}": value for index, value in enumerate(seeds)}}
    rows = await db.execute(
        text(
            f"""
            WITH RECURSIVE descendants(node_id) AS (
              SELECT id FROM provenance_nodes
              WHERE owner_id=:owner_id AND id IN ({bind_names})
              UNION
              SELECT edge.target_node_id
              FROM provenance_edges AS edge
              JOIN descendants ON descendants.node_id=edge.source_node_id
              WHERE edge.owner_id=:owner_id
            )
            SELECT node_id FROM descendants ORDER BY node_id
            """
        ),
        params,
    )
    return {str(row[0]) for row in rows}


async def append_memory_lifecycle_event(
    db: AsyncSession,
    *,
    memory: Memory,
    event_type: str,
    action_key: str,
    from_status: str | None,
    to_status: str,
    from_validity_state: str | None,
    to_validity_state: str,
    reason_code: str = "",
    request_digest: str | None = None,
    expires_at_before: datetime | None = None,
    expires_at_after: datetime | None = None,
    source_run_id: str | None = None,
    source_message_id: int | None = None,
    version: int | None = None,
) -> MemoryLifecycleEvent:
    """Append an audit event and accept only an exact action-key replay."""

    event_version = memory.lifecycle_version if version is None else version
    values = {
        "owner_id": memory.owner_id,
        "memory_id": memory.id,
        "version": event_version,
        "event_type": event_type,
        "action_key": action_key,
        "request_digest": request_digest,
        "from_status": from_status,
        "to_status": to_status,
        "from_validity_state": from_validity_state,
        "to_validity_state": to_validity_state,
        "reason_code": reason_code,
        "expires_at_before": expires_at_before,
        "expires_at_after": expires_at_after,
        "source_run_id": source_run_id,
        "source_message_id": source_message_id,
    }
    await db.execute(
        sqlite_insert(MemoryLifecycleEvent)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["action_key"])
    )
    event = (
        await db.execute(
            select(MemoryLifecycleEvent).where(MemoryLifecycleEvent.action_key == action_key)
        )
    ).scalar_one()
    actual = {
        key: getattr(event, key)
        for key in values
        if key not in {"expires_at_before", "expires_at_after"}
    }
    expected = {
        key: value
        for key, value in values.items()
        if key not in {"expires_at_before", "expires_at_after"}
    }
    def same_instant(left: datetime | None, right: datetime | None) -> bool:
        if left is None or right is None:
            return left is right
        return coerce_legacy_utc(left) == coerce_legacy_utc(right)

    if (
        actual != expected
        or not same_instant(event.expires_at_before, expires_at_before)
        or not same_instant(event.expires_at_after, expires_at_after)
    ):
        raise MemoryLifecycleConflict(f"memory lifecycle action conflict: {action_key}")
    return event


async def _memory_provenance_scope(
    db: AsyncSession,
    memory: Memory,
) -> tuple[int | None, str | None]:
    if memory.scope == "global":
        return None, None
    if memory.scope == "plan":
        if not memory.scope_id or not memory.scope_id.isdigit():
            raise MemoryLifecycleConflict("Memory Plan scope is invalid")
        return int(memory.scope_id), None
    if memory.scope == "session":
        if not memory.scope_id:
            raise MemoryLifecycleConflict("Memory Session scope is invalid")
        session = await db.get(Session, memory.scope_id)
        if session is None or session.owner_id != memory.owner_id:
            raise MemoryLifecycleConflict("Memory Session scope does not exist")
        return session.plan_id, session.id
    raise MemoryLifecycleConflict("Memory scope is invalid")


async def materialize_initial_memory_provenance(
    db: AsyncSession,
    *,
    memory: Memory,
    source_node_id: str | None,
) -> Memory:
    """Attach the exact immutable v1 node before publishing a new Memory."""

    if memory.id is None or memory.lifecycle_version != 1 or memory.provenance_node_id is not None:
        raise MemoryLifecycleConflict("initial Memory provenance requires an unlinked v1 row")
    expected_content_digest = canonical_digest(memory.content)
    if memory.validity_state != "valid" or memory.content_hash != expected_content_digest:
        raise MemoryLifecycleConflict("initial Memory content identity is invalid")
    source_node_ids: list[str] = []
    if source_node_id is not None:
        source = await db.get(ProvenanceNode, source_node_id)
        if source is None or source.owner_id != memory.owner_id:
            raise MemoryLifecycleConflict("initial Memory source is outside owner scope")
        source_node_ids.append(source_node_id)
    expected_source_digest = canonical_digest(source_node_ids)
    if memory.provenance_digest != expected_source_digest:
        raise MemoryLifecycleConflict("initial Memory provenance digest is inconsistent")
    plan_id, session_id = await _memory_provenance_scope(db, memory)
    node = await ensure_provenance_node(
        db,
        owner_id=memory.owner_id,
        plan_id=plan_id,
        session_id=session_id,
        kind="memory",
        entity_key=memory.id,
        entity_version=1,
        content_digest=expected_content_digest,
    )
    if source_node_ids:
        await append_provenance_edges(
            db,
            [
                {
                    "owner_id": memory.owner_id,
                    "source_node_id": source_node_ids[0],
                    "target_node_id": node.id,
                    "relation": "memory_source",
                    "ordinal": 0,
                }
            ],
        )
    memory.provenance_node_id = node.id
    await flush_uow(db)
    return memory


async def _prepare_memory_provenance_transition(
    db: AsyncSession,
    *,
    memory: Memory,
    next_version: int,
    additional_source_node_id: str | None,
) -> tuple[str | None, str]:
    """Build the immutable next-version graph before the fenced row update."""

    if memory.provenance_node_id is None:
        if memory.validity_state == "valid":
            raise MemoryLifecycleConflict(
                "valid Memory is missing its provenance pointer"
            )
        if additional_source_node_id is not None:
            raise MemoryLifecycleConflict(
                "unverified Memory cannot acquire a provenance source during transition"
            )
        return None, memory.provenance_digest
    expected_content_digest = canonical_digest(memory.content)
    if memory.content_hash != expected_content_digest:
        raise MemoryLifecycleConflict("Memory content hash changed")
    current_node = await db.get(ProvenanceNode, memory.provenance_node_id)
    if (
        current_node is None
        or current_node.owner_id != memory.owner_id
        or current_node.kind != "memory"
        or current_node.entity_key != str(memory.id)
        or current_node.entity_version != memory.lifecycle_version
        or current_node.content_digest != expected_content_digest
    ):
        raise MemoryLifecycleConflict("Memory provenance pointer is inconsistent")

    plan_id, session_id = await _memory_provenance_scope(db, memory)
    if (current_node.plan_id, current_node.session_id) != (plan_id, session_id):
        raise MemoryLifecycleConflict("Memory provenance scope is inconsistent")

    source_node_ids = [
        str(value)
        for value in (
            await db.scalars(
                select(ProvenanceEdge.source_node_id)
                .where(
                    ProvenanceEdge.owner_id == memory.owner_id,
                    ProvenanceEdge.target_node_id == current_node.id,
                    ProvenanceEdge.relation == "memory_source",
                )
                .order_by(ProvenanceEdge.ordinal)
            )
        )
    ]
    if memory.provenance_digest != canonical_digest(source_node_ids):
        raise MemoryLifecycleConflict("Memory provenance digest is inconsistent")
    if additional_source_node_id is not None:
        additional = await db.get(ProvenanceNode, additional_source_node_id)
        if additional is None or additional.owner_id != memory.owner_id:
            raise MemoryLifecycleConflict("additional Memory source is outside owner scope")
        source_node_ids.append(additional_source_node_id)
    source_node_ids = list(dict.fromkeys(source_node_ids))

    next_node = await ensure_provenance_node(
        db,
        owner_id=memory.owner_id,
        plan_id=plan_id,
        session_id=session_id,
        kind="memory",
        entity_key=memory.id,
        entity_version=next_version,
        content_digest=expected_content_digest,
    )
    if source_node_ids:
        await append_provenance_edges(
            db,
            [
                {
                    "owner_id": memory.owner_id,
                    "source_node_id": source_node_id,
                    "target_node_id": next_node.id,
                    "relation": "memory_source",
                    "ordinal": ordinal,
                }
                for ordinal, source_node_id in enumerate(source_node_ids)
            ],
        )
    return next_node.id, canonical_digest(source_node_ids)


async def transition_memory(
    db: AsyncSession,
    *,
    owner_id: str,
    memory_id: int,
    expected_version: int,
    event_type: str,
    action_key: str,
    to_status: str | None = None,
    to_validity_state: str | None = None,
    reason_code: str = "",
    request_digest: str | None = None,
    expires_at: datetime | None | object = _UNSET,
    archived_from_status: str | None | object = _UNSET,
    archived_reason: str | object = _UNSET,
    confidence: float | object = _UNSET,
    source_run_id: str | None = None,
    source_message_id: int | None = None,
    additional_source_node_id: str | None = None,
    changed_at: datetime | None = None,
) -> Memory:
    """Apply one fenced semantic Memory transition and append its audit event."""

    transition_digest = memory_transition_digest(
        owner_id=owner_id,
        memory_id=memory_id,
        expected_version=expected_version,
        event_type=event_type,
        action_key=action_key,
        to_status=to_status,
        to_validity_state=to_validity_state,
        reason_code=reason_code,
        expires_at=expires_at,
        archived_from_status=archived_from_status,
        archived_reason=archived_reason,
        confidence=confidence,
        source_run_id=source_run_id,
        source_message_id=source_message_id,
        additional_source_node_id=additional_source_node_id,
    )
    if request_digest is not None and request_digest != transition_digest:
        raise MemoryLifecycleConflict(
            "request_digest must equal the canonical Memory transition digest"
        )
    request_digest = transition_digest

    replay = (
        await db.execute(
            select(MemoryLifecycleEvent).where(MemoryLifecycleEvent.action_key == action_key)
        )
    ).scalar_one_or_none()
    if replay is not None:
        replay_to_status = to_status if to_status is not None else replay.from_status
        replay_to_validity = (
            to_validity_state
            if to_validity_state is not None
            else replay.from_validity_state
        )
        replay_expiry = replay.expires_at_before if expires_at is _UNSET else expires_at

        def same_instant(left: datetime | None, right: datetime | None | object) -> bool:
            if left is None or right is None:
                return left is right
            if right is _UNSET or not isinstance(right, datetime):
                return False
            return coerce_legacy_utc(left) == coerce_legacy_utc(right)

        if (
            replay.owner_id != owner_id
            or replay.memory_id != memory_id
            or replay.request_digest != request_digest
            or replay.event_type != event_type
            or replay.to_status != replay_to_status
            or replay.to_validity_state != replay_to_validity
            or replay.reason_code != reason_code
            or replay.source_run_id != source_run_id
            or replay.source_message_id != source_message_id
            or not same_instant(replay.expires_at_after, replay_expiry)
        ):
            raise MemoryLifecycleConflict(f"memory lifecycle action conflict: {action_key}")
        replayed = await db.get(Memory, memory_id)
        if replayed is None or replayed.owner_id != owner_id:
            raise MemoryLifecycleConflict("memory disappeared after lifecycle replay")
        return replayed

    memory = await db.get(Memory, memory_id)
    if memory is None or memory.owner_id != owner_id:
        raise LookupError("Memory not found")
    if memory.lifecycle_version != expected_version:
        raise MemoryLifecycleConflict(
            f"memory lifecycle version changed: expected {expected_version}, got {memory.lifecycle_version}"
        )
    now = changed_at or utc_now()
    previous_status = memory.status
    previous_validity = memory.validity_state
    previous_expires = memory.expires_at
    next_status = memory.status if to_status is None else to_status
    next_validity = memory.validity_state if to_validity_state is None else to_validity_state
    next_expires = memory.expires_at if expires_at is _UNSET else expires_at
    next_archived_from = (
        memory.archived_from_status
        if archived_from_status is _UNSET
        else archived_from_status
    )
    next_archived_reason = memory.archived_reason if archived_reason is _UNSET else archived_reason
    next_confidence = memory.confidence if confidence is _UNSET else confidence
    invalidated_at = memory.invalidated_at
    invalidation_reason = memory.invalidation_reason
    if next_validity in {"review_required", "invalid"}:
        if not reason_code:
            raise ValueError("invalid Memory validity transition requires reason_code")
        invalidated_at = now
        invalidation_reason = reason_code
    elif next_validity == "valid":
        invalidated_at = None
        invalidation_reason = ""

    next_version = expected_version + 1
    next_provenance_node_id, next_provenance_digest = (
        await _prepare_memory_provenance_transition(
            db,
            memory=memory,
            next_version=next_version,
            additional_source_node_id=additional_source_node_id,
        )
    )
    result = await db.execute(
        update(Memory)
        .where(
            Memory.id == memory_id,
            Memory.owner_id == owner_id,
            Memory.lifecycle_version == expected_version,
        )
        .values(
            status=next_status,
            validity_state=next_validity,
            invalidated_at=invalidated_at,
            invalidation_reason=invalidation_reason,
            expires_at=next_expires,
            archived_from_status=next_archived_from,
            archived_reason=next_archived_reason,
            lifecycle_reason_code=reason_code,
            confidence=next_confidence,
            lifecycle_version=next_version,
            provenance_node_id=next_provenance_node_id,
            provenance_digest=next_provenance_digest,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise MemoryLifecycleConflict("memory lifecycle compare-and-swap lost")
    await db.refresh(memory)
    await append_memory_lifecycle_event(
        db,
        memory=memory,
        version=next_version,
        event_type=event_type,
        action_key=action_key,
        request_digest=request_digest,
        from_status=previous_status,
        to_status=next_status,
        from_validity_state=previous_validity,
        to_validity_state=next_validity,
        reason_code=reason_code,
        expires_at_before=previous_expires,
        expires_at_after=next_expires,
        source_run_id=source_run_id,
        source_message_id=source_message_id,
    )
    await flush_uow(db)
    return memory


__all__ = [
    "MemoryLifecycleConflict",
    "ProvenanceConflict",
    "append_memory_lifecycle_event",
    "append_provenance_edge",
    "append_provenance_edges",
    "bump_context_generation",
    "canonical_digest",
    "canonical_json",
    "ensure_provenance_node",
    "ensure_provenance_nodes",
    "memory_transition_digest",
    "materialize_initial_memory_provenance",
    "provenance_descendant_node_ids",
    "read_context_generation",
    "transition_memory",
]
