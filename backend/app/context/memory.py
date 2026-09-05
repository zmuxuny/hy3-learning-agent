from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import settings
from app.core.time import coerce_legacy_utc, utc_now
from app.db.uow import (
    DatabaseBusyError,
    commit as commit_uow,
    flush as flush_uow,
    run_short_transaction,
)
from app.context.provenance import (
    MemoryLifecycleConflict,
    append_memory_lifecycle_event,
    append_provenance_edges,
    bump_context_generation,
    canonical_digest,
    ensure_provenance_node,
    ensure_provenance_nodes,
    materialize_initial_memory_provenance,
    transition_memory,
)
from app.core.prompt_envelope import ensure_request_fits, request_budget_breakdown
from app.models import (
    AgentRun,
    ChatMessage,
    Memory,
    Plan,
    ProvenanceNode,
    Session,
    SessionCompressionState,
    SessionHandoff,
    SessionSummary,
    Stage,
)
from app.retrieval import get_embedding_provider
from app.retrieval.bm25 import BM25
from app.retrieval.provider import embedding_similarity
from app.retrieval.text import tokenize_terms
from app.services.plans import build_plan_memory_summary


LAYER_WEIGHT = {"semantic": 4.0, "long_term": 4.0, "episodic": 2.5, "short_term": 2.0, "working": 1.0}
_SESSION_WRITE_ACTIVITY_KEY = "h2_session_write_activity"
_COMPRESSION_ALGORITHM_VERSION = "h5-contiguous-chunks-v1"
_COMPRESSION_CLAIM_SECONDS = 3_600
_RETRIEVAL_POLICY_VERSION = "h5-absolute-relevance-v1"
_MIN_ABSOLUTE_RELEVANCE = 0.10
_VECTOR_NOISE_FLOOR = 0.65
_GENERIC_MEMORY_LIMIT = 4


@dataclass(frozen=True)
class _CompressionClaim:
    token: str
    owner_id: str
    session_id: str
    plan_id: int | None
    generation: int
    base_summary: str
    base_summary_id: int | None
    source_manifest: tuple[dict[str, Any], ...]
    source_digest: str


def _message_digest(message: ChatMessage) -> str:
    return canonical_digest(message.content)


def _split_utf8(value: str, max_bytes: int) -> list[str]:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in value:
        encoded_size = len(character.encode("utf-8"))
        if current and current_bytes + encoded_size > max_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += encoded_size
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Keep a UTF-8 prefix without splitting a code point."""

    if max_bytes < 0:
        raise ValueError("max_bytes cannot be negative")
    payload = value.encode("utf-8")
    if len(payload) <= max_bytes:
        return value
    return payload[:max_bytes].decode("utf-8", errors="ignore")


def _compression_system_prompt() -> str:
    return (
        "Compress the learning conversation into concise Chinese factual memory. "
        "Preserve goals, decisions, plan/task IDs, evidence, unresolved blockers, "
        "preferences, and commitments. Incorporate every source byte in this chunk "
        "and do not invent facts."
    )


def _compression_chunk_budget() -> int:
    # Reserve the maximum persisted summary size because each chunk's result is
    # fed into the next request. The shared envelope uses a conservative
    # one-token-per-UTF-8-byte upper bound.
    probe = [
        {"role": "system", "content": _compression_system_prompt()},
        {
            "role": "user",
            "content": (
                "Existing verified summary:\n"
                + ("x" * 12_000)
                + "\n\nSource chunk 999999/999999:\n"
            ),
        },
    ]
    breakdown = request_budget_breakdown(messages=probe, tools=None)
    available = int(breakdown["model_context_window"]) - int(breakdown["total_tokens"])
    if available < 1:
        raise RuntimeError("compression prompt envelope leaves no source-byte budget")
    # JSON can expand one raw ASCII control byte to a six-byte ``\u00xx``
    # escape. Splitting at one sixth of the exact remaining envelope therefore
    # keeps arbitrary UTF-8 source text safe; every concrete request is still
    # checked immediately before provider I/O.
    return max(1, available // 6)


def _compression_chunks(
    manifest: tuple[dict[str, Any], ...],
    *,
    max_bytes: int,
) -> tuple[list[str], dict[int, int]]:
    chunks: list[str] = []
    message_chunk_counts: dict[int, int] = {}
    current = ""
    current_bytes = 0
    for item in manifest:
        message_id = int(item["id"])
        envelope = (
            f"MESSAGE id={message_id} version={int(item['version'])} role={item['role']}\n"
            f"{item['content']}\nEND MESSAGE id={message_id}\n"
        )
        parts = _split_utf8(envelope, max_bytes)
        message_chunk_counts[message_id] = len(parts)
        for part in parts:
            part_bytes = len(part.encode("utf-8"))
            if current and current_bytes + part_bytes > max_bytes:
                chunks.append(current)
                current = ""
                current_bytes = 0
            if part_bytes >= max_bytes:
                if current:
                    chunks.append(current)
                    current = ""
                    current_bytes = 0
                chunks.append(part)
            else:
                current += part
                current_bytes += part_bytes
    if current:
        chunks.append(current)
    return chunks, message_chunk_counts


def _fallback_summary(
    *,
    base_summary: str,
    chunks: list[str],
    source_digest: str,
) -> str:
    """Read every source chunk and emit an explicitly lossy local digest.

    Runtime model-backed compression is preferred. This deterministic path is
    retained for offline operation, but unlike the legacy ``last 12`` fallback
    it consumes every source byte and records a digest over every chunk before
    the durable coverage cursor can advance.
    """

    chunk_digests: list[str] = []
    samples: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_digests.append(canonical_digest(chunk))
        normalized = " ".join(chunk.split())
        sample = _truncate_utf8(normalized, 240)
        samples.append(f"分块 {index}/{len(chunks)}：{sample}")
    digest = canonical_digest(
        {
            "source_digest": source_digest,
            "chunk_digests": chunk_digests,
        }
    )
    header = (
        "历史对话压缩（离线确定性完整读取）："
        f"source_sha256={source_digest}；chunk_set_sha256={digest}；"
        f"chunks={len(chunks)}。"
    )
    # Preserve the previous verified summary, the immutable whole-source
    # identity, and a bounded sample from each processed chunk. If samples do
    # not all fit, the digest still commits to the complete byte stream.
    summary = "\n".join(part for part in [header, base_summary, *samples] if part)
    return _truncate_utf8(summary, 12_000)


def _absolute_relevance(
    query_terms: list[str],
    document_terms: list[str],
    vector_score: float,
) -> tuple[float, float, float]:
    query_set = set(query_terms)
    lexical = (
        len(query_set.intersection(document_terms)) / len(query_set)
        if query_set
        else 0.0
    )
    semantic = max(
        0.0,
        min(1.0, (vector_score - _VECTOR_NOISE_FLOOR) / (1.0 - _VECTOR_NOISE_FLOOR)),
    )
    return max(lexical, semantic), lexical, semantic


def _quota_select(
    scored: list[tuple[float, Memory, dict[str, Any]]],
    *,
    limit: int,
) -> list[tuple[float, Memory, dict[str, Any]]]:
    if limit <= 0:
        return []
    ordered = sorted(
        scored,
        key=lambda item: (
            -item[0],
            -coerce_legacy_utc(item[1].updated_at).timestamp(),
            item[1].id,
        ),
    )
    selected: list[tuple[float, Memory, dict[str, Any]]] = []
    selected_ids: set[int] = set()

    # Reserve one relevant Global fact and, when present, one focused Plan
    # fact. This prevents a large Session tail from evicting durable identity.
    reserve_scopes = ["global"] + (["plan"] if limit >= 2 else [])
    for scope in reserve_scopes:
        candidate = next((item for item in ordered if item[1].scope == scope), None)
        if candidate is not None and candidate[1].id not in selected_ids:
            selected.append(candidate)
            selected_ids.add(candidate[1].id)
            if len(selected) >= limit:
                break

    scope_caps = {
        "session": max(1, (limit * 3 + 4) // 5),
        "plan": max(1, (limit * 4 + 4) // 5),
    }
    layer_caps = {
        "working": max(1, (limit + 4) // 5),
        "short_term": max(1, (limit * 2 + 4) // 5),
        "episodic": max(1, (limit * 3 + 4) // 5),
        "long_term": max(1, (limit * 4 + 4) // 5),
        "semantic": max(1, (limit * 4 + 4) // 5),
    }
    for item in ordered:
        memory = item[1]
        if memory.id in selected_ids or len(selected) >= limit:
            continue
        scope_count = sum(existing[1].scope == memory.scope for existing in selected)
        layer_count = sum(existing[1].layer == memory.layer for existing in selected)
        if (
            (memory.scope in scope_caps and scope_count >= scope_caps[memory.scope])
            or (memory.layer in layer_caps and layer_count >= layer_caps[memory.layer])
        ):
            continue
        selected.append(item)
        selected_ids.add(memory.id)
    selected.sort(
        key=lambda item: (
            -item[0],
            -coerce_legacy_utc(item[1].updated_at).timestamp(),
            item[1].id,
        )
    )
    for _, memory, breakdown in selected:
        breakdown["quota_bucket"] = f"{memory.scope}:{memory.layer}"
    return selected[:limit]


def _clear_compression_claim_values() -> dict[str, Any]:
    return {
        "claim_token": None,
        "claim_owner": None,
        "claim_generation": None,
        "claim_start_message_id": None,
        "claim_end_message_id": None,
        "claim_base_summary_id": None,
        "claim_source_manifest": None,
        "claim_source_digest": None,
        "claim_algorithm_version": None,
        "claim_started_at": None,
        "claim_expires_at": None,
    }


async def _compression_source(
    db: AsyncSession,
    *,
    session_id: str,
) -> tuple[Session, SessionCompressionState, list[ChatMessage]] | None:
    session = await db.get(Session, session_id)
    if session is None:
        return None
    await db.execute(
        sqlite_insert(SessionCompressionState)
        .values(session_id=session.id, owner_id=session.owner_id, generation=0)
        .on_conflict_do_nothing(index_elements=["session_id"])
    )
    state = await db.get(SessionCompressionState, session.id)
    if state is None or state.owner_id != session.owner_id:
        raise RuntimeError("Session compression state could not be established")
    messages = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == session.id,
                    ChatMessage.validity_state == "active",
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).scalars()
    )
    messages = [
        message
        for message in messages
        if not (message.message_metadata or {}).get("superseded_by_edit")
    ]
    keep = settings.AGENT_RECENT_MESSAGE_LIMIT
    threshold = min(settings.AGENT_SESSION_COMPRESSION_THRESHOLD, keep)
    if len(messages) <= threshold:
        return None
    candidates = messages[:-keep]
    if state.covered_through_message_id is not None:
        cursor = await db.get(ChatMessage, state.covered_through_message_id)
        if (
            cursor is None
            or cursor.session_id != session.id
            or cursor.version != state.covered_through_message_version
        ):
            # An edit coordinator must invalidate/reset this generation before
            # another summary can advance it. Never guess a new cursor.
            return None
        cursor_position = (coerce_legacy_utc(cursor.created_at), cursor.id)
        candidates = [
            message
            for message in candidates
            if (coerce_legacy_utc(message.created_at), message.id) > cursor_position
        ]
    if not candidates:
        return None
    return session, state, candidates


def _claim_manifest(messages: list[ChatMessage]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": message.id,
            "version": message.version,
            "role": message.role,
            "content": message.content,
            "content_digest": _message_digest(message),
            "source_bytes": len(message.content.encode("utf-8")),
        }
        for message in messages
    )


def _persisted_manifest(manifest: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "version": item["version"],
            "role": item["role"],
            "content_digest": item["content_digest"],
            "source_bytes": item["source_bytes"],
        }
        for item in manifest
    ]


async def _acquire_compression_claim(
    db: AsyncSession,
    *,
    session_id: str,
) -> _CompressionClaim | None:
    source = await _compression_source(db, session_id=session_id)
    if source is None:
        return None
    session, state, messages = source
    now = utc_now()
    if (
        state.claim_token is not None
        and state.claim_expires_at is not None
        and coerce_legacy_utc(state.claim_expires_at) > now
    ):
        return None
    manifest = _claim_manifest(messages)
    persisted_manifest = _persisted_manifest(manifest)
    source_digest = canonical_digest(persisted_manifest)
    base_summary = (
        await db.execute(
            select(SessionSummary)
            .where(
                SessionSummary.session_id == session.id,
                SessionSummary.validity_state == "valid",
            )
            .order_by(SessionSummary.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    token = uuid4().hex
    result = await db.execute(
        update(SessionCompressionState)
        .where(
            SessionCompressionState.session_id == session.id,
            SessionCompressionState.generation == state.generation,
            or_(
                SessionCompressionState.claim_token.is_(None),
                SessionCompressionState.claim_expires_at <= now,
            ),
        )
        .values(
            claim_token=token,
            claim_owner=f"compressor:{token}",
            claim_generation=state.generation,
            claim_start_message_id=messages[0].id,
            claim_end_message_id=messages[-1].id,
            claim_base_summary_id=base_summary.id if base_summary else None,
            claim_source_manifest=persisted_manifest,
            claim_source_digest=source_digest,
            claim_algorithm_version=_COMPRESSION_ALGORITHM_VERSION,
            claim_started_at=now,
            claim_expires_at=now + timedelta(seconds=_COMPRESSION_CLAIM_SECONDS),
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        return None
    return _CompressionClaim(
        token=token,
        owner_id=session.owner_id,
        session_id=session.id,
        plan_id=session.plan_id,
        generation=state.generation,
        base_summary=base_summary.content if base_summary else session.summary,
        base_summary_id=base_summary.id if base_summary else None,
        source_manifest=manifest,
        source_digest=source_digest,
    )


async def _release_compression_claim(
    db: AsyncSession,
    *,
    claim: _CompressionClaim,
) -> None:
    await db.execute(
        update(SessionCompressionState)
        .where(
            SessionCompressionState.session_id == claim.session_id,
            SessionCompressionState.claim_token == claim.token,
            SessionCompressionState.generation == claim.generation,
        )
        .values(**_clear_compression_claim_values(), updated_at=utc_now())
    )


async def _renew_compression_claim(
    db: AsyncSession,
    *,
    claim: _CompressionClaim,
) -> bool:
    """Extend one live claim between provider calls without holding a wait txn."""

    now = utc_now()
    result = await db.execute(
        update(SessionCompressionState)
        .where(
            SessionCompressionState.session_id == claim.session_id,
            SessionCompressionState.owner_id == claim.owner_id,
            SessionCompressionState.claim_token == claim.token,
            SessionCompressionState.claim_generation == claim.generation,
            SessionCompressionState.generation == claim.generation,
            SessionCompressionState.claim_source_digest == claim.source_digest,
        )
        .values(
            claim_expires_at=now + timedelta(seconds=_COMPRESSION_CLAIM_SECONDS),
            updated_at=now,
        )
    )
    return result.rowcount == 1


async def _finalize_compression(
    db: AsyncSession,
    *,
    claim: _CompressionClaim,
    summary_content: str,
    method: str,
    message_chunk_counts: dict[int, int],
    require_claim: bool = True,
) -> bool:
    state = await db.get(SessionCompressionState, claim.session_id)
    if state is None or state.owner_id != claim.owner_id:
        return False
    if require_claim and (
        state.claim_token != claim.token
        or state.claim_generation != claim.generation
        or state.generation != claim.generation
        or state.claim_source_digest != claim.source_digest
        or list(state.claim_source_manifest or []) != _persisted_manifest(claim.source_manifest)
    ):
        return False
    message_ids = [int(item["id"]) for item in claim.source_manifest]
    current_messages = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.id.in_(message_ids))
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).scalars()
    )
    if len(current_messages) != len(message_ids):
        if require_claim:
            await _release_compression_claim(db, claim=claim)
        return False
    for current, expected in zip(current_messages, claim.source_manifest, strict=True):
        if (
            current.id != expected["id"]
            or current.session_id != claim.session_id
            or current.version != expected["version"]
            or current.role != expected["role"]
            or current.validity_state != "active"
            or (current.message_metadata or {}).get("superseded_by_edit")
            or _message_digest(current) != expected["content_digest"]
            or len(current.content.encode("utf-8")) != expected["source_bytes"]
        ):
            if require_claim:
                await _release_compression_claim(db, claim=claim)
            return False

    session = await db.get(Session, claim.session_id)
    if session is None or session.owner_id != claim.owner_id:
        if require_claim:
            await _release_compression_claim(db, claim=claim)
        return False
    base_summary: SessionSummary | None = None
    if claim.base_summary_id is not None:
        base_summary = await db.get(SessionSummary, claim.base_summary_id)
        if (
            base_summary is None
            or base_summary.owner_id != claim.owner_id
            or base_summary.session_id != claim.session_id
            or base_summary.validity_state != "valid"
            or base_summary.provenance_node_id is None
        ):
            if require_claim:
                await _release_compression_claim(db, claim=claim)
            return False
    max_version = int(
        (
            await db.scalar(
                select(SessionSummary.version)
                .where(SessionSummary.session_id == claim.session_id)
                .order_by(SessionSummary.version.desc())
                .limit(1)
            )
        )
        or 0
    )
    summary = SessionSummary(
        owner_id=claim.owner_id,
        session_id=claim.session_id,
        version=max_version + 1,
        content=summary_content,
        coverage_start_message_id=message_ids[0],
        covered_through_message_id=message_ids[-1],
        coverage_count=len(message_ids),
        source_message_ids=message_ids,
        method=method,
        source_digest=claim.source_digest,
        content_hash=canonical_digest(summary_content),
        algorithm_version=_COMPRESSION_ALGORITHM_VERSION,
        validity_state="building",
    )
    db.add(summary)
    await flush_uow(db)
    message_nodes = await ensure_provenance_nodes(
        db,
        [
            {
                "owner_id": claim.owner_id,
                "plan_id": claim.plan_id,
                "session_id": claim.session_id,
                "kind": "message",
                "entity_key": item["id"],
                "entity_version": item["version"],
                "content_digest": item["content_digest"],
            }
            for item in claim.source_manifest
        ],
    )
    summary_node = await ensure_provenance_node(
        db,
        owner_id=claim.owner_id,
        plan_id=claim.plan_id,
        session_id=claim.session_id,
        kind="session_summary",
        entity_key=summary.id,
        entity_version=summary.version,
        content_digest=summary.content_hash,
    )
    await append_provenance_edges(
        db,
        [
            {
                "owner_id": claim.owner_id,
                "source_node_id": node.id,
                "target_node_id": summary_node.id,
                "relation": "summary_source",
                "ordinal": ordinal,
                "source_bytes": int(item["source_bytes"]),
                "read_bytes": int(item["source_bytes"]),
                "chunk_count": max(1, int(message_chunk_counts.get(int(item["id"]), 1))),
            }
            for ordinal, (item, node) in enumerate(
                zip(claim.source_manifest, message_nodes, strict=True)
            )
        ],
    )
    if base_summary is not None:
        await append_provenance_edges(
            db,
            [
                {
                    "owner_id": claim.owner_id,
                    "source_node_id": base_summary.provenance_node_id,
                    "target_node_id": summary_node.id,
                    "relation": "summary_base",
                    "ordinal": 0,
                }
            ],
        )
    summary.provenance_node_id = summary_node.id
    summary.validity_state = "valid"
    session.summary = summary_content
    for message in current_messages:
        message.message_metadata = {
            **(message.message_metadata or {}),
            "included_in_summary": True,
        }
    state.covered_through_message_id = message_ids[-1]
    state.covered_through_message_version = int(claim.source_manifest[-1]["version"])
    state.generation = claim.generation + 1
    for field, value in _clear_compression_claim_values().items():
        setattr(state, field, value)
    state.updated_at = utc_now()
    await bump_context_generation(db, claim.owner_id)
    await flush_uow(db)
    return True


async def _ensure_memory_source_node(
    db: AsyncSession,
    *,
    memory: Memory,
    source_type: str,
    source_id: str | None,
):
    if source_type in {"user", "manual"}:
        if source_id is not None:
            raise ValueError("User-authored Memory source_id must be empty")
        return None
    if not source_id:
        raise ValueError("Durable Memory source requires source_id")

    async def checked(node):
        if memory.scope == "global":
            if node.plan_id is not None:
                raise ValueError(
                    "Plan-private source cannot create a Global Memory"
                )
        elif memory.scope == "plan":
            target_plan_id = int(memory.scope_id or "0")
            if node.plan_id is not None and node.plan_id != target_plan_id:
                raise ValueError("Memory source is outside the Plan scope")
        elif memory.scope == "session":
            target_session = await db.get(Session, memory.scope_id)
            if target_session is None or target_session.owner_id != memory.owner_id:
                raise ValueError("Memory Session scope does not exist")
            if node.plan_id is not None and node.plan_id != target_session.plan_id:
                raise ValueError("Memory source is outside the Session Plan scope")
            if node.session_id is not None and node.session_id != target_session.id:
                raise ValueError("Memory source is outside the Session scope")
        return node

    if source_type in {"agent_run", "run"}:
        run = await db.get(AgentRun, source_id)
        if run is None or run.owner_id != memory.owner_id:
            raise ValueError("Memory source Run does not exist")
        return await checked(
            await ensure_provenance_node(
                db,
                owner_id=memory.owner_id,
                plan_id=run.plan_id,
                session_id=run.session_id,
                kind="agent_run",
                entity_key=run.id,
                entity_version=1,
                content_digest=canonical_digest(
                    {
                        "id": run.id,
                        "objective": run.objective,
                        "trigger": run.trigger,
                        "created_at": run.created_at,
                    }
                ),
            ),
        )
    if source_type in {"message", "chat_message"}:
        try:
            message_id = int(source_id)
        except ValueError as exc:
            raise ValueError("Memory source Message id is invalid") from exc
        message = await db.get(ChatMessage, message_id)
        if (
            message is None
            or message.validity_state != "active"
            or (message.message_metadata or {}).get("superseded_by_edit")
        ):
            raise ValueError("Memory source Message does not exist")
        source_session = await db.get(Session, message.session_id)
        if source_session is None or source_session.owner_id != memory.owner_id:
            raise ValueError("Memory source Message owner mismatch")
        return await checked(
            await ensure_provenance_node(
                db,
                owner_id=memory.owner_id,
                plan_id=source_session.plan_id,
                session_id=source_session.id,
                kind="message",
                entity_key=message.id,
                entity_version=message.version,
                content_digest=canonical_digest(message.content),
            )
        )
    if source_type in {"session_summary", "summary"}:
        try:
            summary_id = int(source_id)
        except ValueError as exc:
            raise ValueError("Memory source Summary id is invalid") from exc
        summary = await db.get(SessionSummary, summary_id)
        if (
            summary is None
            or summary.owner_id != memory.owner_id
            or summary.validity_state != "valid"
            or summary.provenance_node_id is None
            or summary.content_hash != canonical_digest(summary.content)
        ):
            raise ValueError("Memory source Summary is not verified")
        source_session = await db.get(Session, summary.session_id)
        if source_session is None or source_session.owner_id != memory.owner_id:
            raise ValueError("Memory source Summary owner mismatch")
        node = await db.get(ProvenanceNode, summary.provenance_node_id)
        if (
            node is None
            or node.owner_id != memory.owner_id
            or node.plan_id != source_session.plan_id
            or node.session_id != source_session.id
            or node.kind != "session_summary"
            or node.entity_key != str(summary.id)
            or node.entity_version != summary.version
            or node.content_digest != canonical_digest(summary.content)
        ):
            raise ValueError("Memory source Summary provenance is inconsistent")
        return await checked(node)
    if source_type in {"session_handoff", "handoff"}:
        handoff = await db.get(SessionHandoff, source_id)
        if (
            handoff is None
            or handoff.owner_id != memory.owner_id
            or handoff.validity_state != "valid"
            or handoff.provenance_node_id is None
            or handoff.content_hash != canonical_digest(handoff.content)
        ):
            raise ValueError("Memory source handoff is not verified")
        node = await db.get(ProvenanceNode, handoff.provenance_node_id)
        if (
            node is None
            or node.owner_id != memory.owner_id
            or node.plan_id != handoff.plan_id
            or node.session_id != handoff.target_session_id
            or node.kind != "session_handoff"
            or node.entity_key != str(handoff.id)
            or node.entity_version != handoff.version
            or node.content_digest != canonical_digest(handoff.content)
        ):
            raise ValueError("Memory source handoff provenance is inconsistent")
        return await checked(node)
    raise ValueError("Unsupported durable Memory source_type")


def _has_pending_session_writes(db: AsyncSession) -> bool:
    return bool(
        db.new
        or db.dirty
        or db.deleted
        or db.sync_session.info.get(_SESSION_WRITE_ACTIVITY_KEY)
    )


def search_terms(text: str) -> set[str]:
    return {term for term in tokenize_terms(text) if term}


def _rank_map(scores: list[float]) -> list[int]:
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    ranks = [0] * len(scores)
    for position, index in enumerate(order):
        ranks[index] = position + 1
    return ranks


def _rrf(ranks: list[int], k: int = 60) -> float:
    return sum(1.0 / (k + rank) for rank in ranks if rank > 0)


class MemoryManager:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def retrieve(
        self,
        owner_id: str,
        *,
        plan_id: int | None,
        session_id: str | None = None,
        query: str,
        limit: int = 20,
    ) -> list[Memory]:
        memories, _ = await self.retrieve_with_scores(
            owner_id,
            plan_id=plan_id,
            session_id=session_id,
            query=query,
            limit=limit,
        )
        return memories

    async def retrieve_with_scores(
        self,
        owner_id: str,
        *,
        plan_id: int | None,
        session_id: str | None = None,
        query: str,
        limit: int = 20,
    ) -> tuple[list[Memory], list[dict[str, Any]]]:
        if self.db.in_nested_transaction():
            raise RuntimeError("memory retrieval cannot coordinate inside a SAVEPOINT")
        if limit < 1:
            return [], []
        now = utc_now()
        query_terms = tokenize_terms(query)
        provider = get_embedding_provider()
        if _has_pending_session_writes(self.db):
            raise RuntimeError(
                "memory retrieval requires a clean session before internal telemetry"
            )
        scope_predicates = [Memory.scope == "global"]
        if plan_id is not None:
            scope_predicates.append(
                and_(Memory.scope == "plan", Memory.scope_id == str(plan_id))
            )
        if session_id is not None:
            scope_predicates.append(
                and_(Memory.scope == "session", Memory.scope_id == session_id)
            )
        result = await self.db.execute(
            select(Memory)
            .where(
                Memory.owner_id == owner_id,
                Memory.status == "confirmed",
                Memory.validity_state == "valid",
                Memory.provenance_node_id.is_not(None),
                func.length(Memory.content_hash) == 64,
                func.length(Memory.provenance_digest) == 64,
                or_(Memory.expires_at.is_(None), Memory.expires_at > now),
                or_(*scope_predicates),
            )
            .order_by(Memory.id)
        )
        candidates = list(result.scalars())
        if not query_terms:
            # A punctuation-only/general query is not permission to inject the
            # focused Session tail. Carry only a bounded Global profile.
            candidates = [memory for memory in candidates if memory.scope == "global"]

        if not candidates:
            return [], []
        candidate_versions = {
            memory.id: int(memory.lifecycle_version) for memory in candidates
        }

        if provider is not None and query_terms:
            # Candidate loading opened a read transaction. Release the
            # snapshot before a local/remote embedding provider can block.
            # The entry guard above guarantees this cannot commit caller work.
            await commit_uow(self.db)
        query_embedding = (
            await asyncio.to_thread(provider.embed, query)
            if provider is not None and query_terms
            else None
        )

        documents = [tokenize_terms(memory.content) for memory in candidates]
        bm25 = BM25()
        bm25.fit(documents)
        bm25_scores = [bm25.score(query_terms, index) for index in range(len(candidates))]

        vector_scores: list[float] = []
        for memory in candidates:
            memory_embedding = memory.embedding
            if memory_embedding is None and provider is not None and query_terms:
                memory_embedding = await asyncio.to_thread(provider.embed, memory.content)
            vector_scores.append(
                embedding_similarity(query_embedding, memory_embedding) if query_embedding else 0.0
            )

        relevance: list[tuple[float, float, float]] = [
            _absolute_relevance(query_terms, documents[index], vector_scores[index])
            for index in range(len(candidates))
        ]
        eligible_indexes = [
            index
            for index, (absolute, _, _) in enumerate(relevance)
            if not query_terms or absolute >= _MIN_ABSOLUTE_RELEVANCE
        ]
        if not eligible_indexes:
            return [], []
        eligible_bm25 = [bm25_scores[index] for index in eligible_indexes]
        eligible_vector = [vector_scores[index] for index in eligible_indexes]
        rank_lists: list[list[int]] = []
        if max(eligible_bm25, default=0.0) > 0:
            rank_lists.append(_rank_map(eligible_bm25))
        if max(eligible_vector, default=0.0) > 0:
            rank_lists.append(_rank_map(eligible_vector))

        scored: list[tuple[float, Memory, dict[str, Any]]] = []
        for eligible_position, index in enumerate(eligible_indexes):
            memory = candidates[index]
            ranks = [rank_list[eligible_position] for rank_list in rank_lists]
            rrf = _rrf(ranks)
            hybrid = (rrf / (len(rank_lists) / 61) * 100) if rank_lists else 0.0
            age_days = max(0, (now - coerce_legacy_utc(memory.updated_at)).days)
            recency = max(0.0, 2.0 - age_days / 30)
            scope_bonus = 3.0 if memory.scope in {"plan", "session"} else 1.0
            layer_weight = LAYER_WEIGHT.get(memory.layer, 1.0)
            total = hybrid + scope_bonus + layer_weight + recency + memory.confidence
            breakdown = {
                "memory_id": memory.id,
                "bm25": round(bm25_scores[index], 4),
                "vector": round(vector_scores[index], 4),
                "relevance": round(relevance[index][0], 4),
                "lexical_relevance": round(relevance[index][1], 4),
                "semantic_relevance": round(relevance[index][2], 4),
                "relevance_threshold": _MIN_ABSOLUTE_RELEVANCE,
                "policy_version": _RETRIEVAL_POLICY_VERSION,
                "rrf": round(rrf, 4),
                "hybrid": round(hybrid, 4),
                "scope": scope_bonus,
                "layer": layer_weight,
                "recency": round(recency, 4),
                "confidence": memory.confidence,
                "total": round(total, 4),
            }
            scored.append((total, memory, breakdown))

        ranked = _quota_select(
            scored,
            limit=min(limit, _GENERIC_MEMORY_LIMIT) if not query_terms else limit,
        )

        # Embedding work happened without a database transaction. Revalidate
        # every lifecycle fence and scope before returning or recording use.
        revalidation_now = utc_now()
        ranked_ids = [item[1].id for item in ranked]
        current_rows = list(
            (
                await self.db.execute(
                    select(
                        Memory.id,
                        Memory.lifecycle_version,
                        Memory.status,
                        Memory.validity_state,
                        Memory.provenance_node_id,
                        Memory.content_hash,
                        Memory.provenance_digest,
                        Memory.expires_at,
                        Memory.scope,
                        Memory.scope_id,
                    ).where(Memory.id.in_(ranked_ids), Memory.owner_id == owner_id)
                )
            ).all()
        )
        current_by_id = {int(row.id): row for row in current_rows}

        def still_visible(memory: Memory) -> bool:
            row = current_by_id.get(memory.id)
            if row is None:
                return False
            if (
                int(row.lifecycle_version) != candidate_versions[memory.id]
                or row.status != "confirmed"
                or row.validity_state != "valid"
                or row.provenance_node_id is None
                or len(row.content_hash) != 64
                or len(row.provenance_digest) != 64
                or (
                    row.expires_at is not None
                    and coerce_legacy_utc(row.expires_at) <= revalidation_now
                )
            ):
                return False
            return row.scope == "global" or (
                plan_id is not None
                and row.scope == "plan"
                and row.scope_id == str(plan_id)
            ) or (
                session_id is not None
                and row.scope == "session"
                and row.scope_id == session_id
            )

        ranked = [item for item in ranked if still_visible(item[1])]
        ranked_memories = [item[1] for item in ranked]
        if ranked_memories and self.db.bind is not None:
            # Release the revalidation read snapshot before a separate short
            # writer records telemetry. No caller writes can be pending here:
            # the entry guard rejected them before provider I/O.
            await commit_uow(self.db)
            access_sessions = async_sessionmaker(
                self.db.bind,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            async def record_access(short_db: AsyncSession) -> list[int]:
                updated_ids: list[int] = []
                for memory in ranked_memories:
                    result = await short_db.execute(
                        update(Memory)
                        .where(
                            Memory.id == memory.id,
                            Memory.owner_id == owner_id,
                            Memory.lifecycle_version == candidate_versions[memory.id],
                            Memory.status == "confirmed",
                            Memory.validity_state == "valid",
                            Memory.provenance_node_id.is_not(None),
                            func.length(Memory.content_hash) == 64,
                            func.length(Memory.provenance_digest) == 64,
                            or_(
                                Memory.expires_at.is_(None),
                                Memory.expires_at > revalidation_now,
                            ),
                        )
                        .values(
                            last_accessed_at=revalidation_now,
                            access_count=Memory.access_count + 1,
                        )
                    )
                    if result.rowcount == 1:
                        updated_ids.append(memory.id)
                return updated_ids

            try:
                updated_ids = await run_short_transaction(access_sessions, record_access)
            except DatabaseBusyError:
                # Access telemetry is not a domain fact and must never make a
                # read unavailable. A later retrieval can update it again.
                pass
            else:
                updated_set = set(updated_ids)
                ranked = [item for item in ranked if item[1].id in updated_set]
                ranked_memories = [item[1] for item in ranked]
                for memory in ranked_memories:
                    set_committed_value(memory, "last_accessed_at", revalidation_now)
                    set_committed_value(
                        memory,
                        "access_count",
                        (memory.access_count or 0) + 1,
                    )
        return ranked_memories, [item[2] for item in ranked]

    async def propose(
        self,
        owner_id: str,
        *,
        scope: str,
        scope_id: str | None,
        layer: str,
        content: str,
        source_type: str,
        source_id: str | None = None,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
        supersedes_id: int | None = None,
    ) -> tuple[Memory, bool]:
        """Create a reviewable memory, or reinforce an identical active memory.

        The boolean indicates whether an existing row was reused. This keeps model
        retries and repeated user statements from growing duplicate long-term memory.
        """
        normalized = _normalize_memory(content)
        if not normalized:
            raise ValueError("Memory content cannot be empty")
        if scope == "global":
            if scope_id is not None:
                raise ValueError("Global memory cannot have scope_id")
        elif scope == "plan":
            if not scope_id or not scope_id.isdigit():
                raise ValueError("Plan memory requires a valid plan scope_id")
            plan = await self.db.get(Plan, int(scope_id))
            if not plan or plan.owner_id != owner_id:
                raise ValueError("Plan memory scope does not exist")
        elif scope == "session":
            if not scope_id:
                raise ValueError("Session memory requires scope_id")
            session = await self.db.get(Session, scope_id)
            if not session or session.owner_id != owner_id:
                raise ValueError("Session memory scope does not exist")
        else:
            raise ValueError("Unsupported memory scope")
        result = await self.db.execute(
            select(Memory).where(
                Memory.owner_id == owner_id,
                Memory.scope == scope,
                Memory.layer == layer,
                Memory.status.in_(["proposed", "confirmed"]),
                Memory.validity_state == "valid",
                Memory.provenance_node_id.is_not(None),
                func.length(Memory.content_hash) == 64,
                func.length(Memory.provenance_digest) == 64,
            )
        )
        for existing in result.scalars():
            if existing.scope_id == scope_id and _normalize_memory(existing.content) == normalized:
                now = utc_now()
                source_node = await _ensure_memory_source_node(
                    self.db,
                    memory=existing,
                    source_type=source_type,
                    source_id=source_id,
                )
                existing = await transition_memory(
                    self.db,
                    owner_id=owner_id,
                    memory_id=existing.id,
                    expected_version=existing.lifecycle_version,
                    event_type="reinforced",
                    action_key=(
                        f"memory:{existing.id}:reinforced:v{existing.lifecycle_version + 1}"
                    ),
                    to_status=existing.status,
                    to_validity_state=existing.validity_state,
                    confidence=max(existing.confidence, confidence),
                    source_run_id=source_id if source_type in {"agent_run", "run"} else None,
                    source_message_id=(
                        int(source_id)
                        if source_id and source_type in {"message", "chat_message"}
                        else None
                    ),
                    additional_source_node_id=(
                        source_node.id if source_node is not None else None
                    ),
                    changed_at=now,
                )
                existing.last_reinforced_at = now
                await bump_context_generation(self.db, owner_id)
                await flush_uow(self.db)
                return existing, True

        if supersedes_id is not None:
            previous = await self.db.get(Memory, supersedes_id)
            if not previous or previous.owner_id != owner_id:
                raise ValueError("Superseded memory not found")
            if (
                previous.status not in {"confirmed", "proposed"}
                or previous.validity_state != "valid"
            ):
                raise ValueError("Only active memory can be corrected")
            if previous.scope != scope or previous.scope_id != scope_id or previous.layer != layer:
                raise ValueError("A correction must keep the original memory scope and layer")

        memory = Memory(
            owner_id=owner_id,
            scope=scope,
            scope_id=scope_id,
            layer=layer,
            content=content.strip(),
            source_type=source_type,
            source_id=source_id,
            confidence=confidence,
            status="proposed",
            expires_at=expires_at,
            supersedes_id=supersedes_id,
            lifecycle_version=1,
            validity_state="valid",
            content_hash=canonical_digest(content.strip()),
            provenance_digest=canonical_digest([]),
        )
        source_node = await _ensure_memory_source_node(
            self.db,
            memory=memory,
            source_type=source_type,
            source_id=source_id,
        )
        memory.provenance_digest = canonical_digest(
            [source_node.id] if source_node is not None else []
        )
        self.db.add(memory)
        await flush_uow(self.db)
        await materialize_initial_memory_provenance(
            self.db,
            memory=memory,
            source_node_id=source_node.id if source_node else None,
        )
        await append_memory_lifecycle_event(
            self.db,
            memory=memory,
            event_type="proposed",
            action_key=f"memory:{memory.id}:proposed:v1",
            request_digest=canonical_digest(
                {
                    "scope": scope,
                    "scope_id": scope_id,
                    "layer": layer,
                    "content": content.strip(),
                    "source_type": source_type,
                    "source_id": source_id,
                    "confidence": confidence,
                    "expires_at": expires_at,
                    "supersedes_id": supersedes_id,
                }
            ),
            from_status=None,
            to_status="proposed",
            from_validity_state=None,
            to_validity_state="valid",
            expires_at_after=expires_at,
            source_run_id=source_id if source_type in {"agent_run", "run"} else None,
            source_message_id=(
                int(source_id)
                if source_id and source_type in {"message", "chat_message"}
                else None
            ),
        )
        await bump_context_generation(self.db, owner_id)
        return memory, False

    async def confirm(self, owner_id: str, memory_id: int) -> Memory:
        memory = await self.db.get(Memory, memory_id)
        if not memory or memory.owner_id != owner_id:
            raise LookupError("Memory not found")
        if memory.status == "confirmed":
            return memory
        if memory.status != "proposed":
            raise ValueError("Only proposed memory can be confirmed")

        now = utc_now()
        if memory.supersedes_id is not None:
            previous = await self.db.get(Memory, memory.supersedes_id)
            if not previous or previous.owner_id != owner_id:
                raise ValueError("Superseded memory not found")
            if (
                previous.status not in {"confirmed", "proposed"}
                or previous.validity_state != "valid"
            ):
                raise ValueError("The original memory changed before this correction was confirmed")
            if previous.scope != memory.scope or previous.scope_id != memory.scope_id:
                raise ValueError("A correction must keep the original memory scope")
            previous_status = previous.status
            previous = await transition_memory(
                self.db,
                owner_id=owner_id,
                memory_id=previous.id,
                expected_version=previous.lifecycle_version,
                event_type="superseded",
                action_key=f"memory:{previous.id}:superseded-by:{memory.id}:v{previous.lifecycle_version + 1}",
                to_status="superseded",
                to_validity_state=previous.validity_state,
                reason_code="confirmed_correction",
                archived_from_status=previous_status,
                archived_reason="由用户确认的新认识替代",
                changed_at=now,
            )
            previous.superseded_by_id = memory.id
            competing = list((await self.db.execute(
                select(Memory).where(
                    Memory.owner_id == owner_id,
                    Memory.supersedes_id == previous.id,
                    Memory.status == "proposed",
                    Memory.id != memory.id,
                )
            )).scalars())
            for proposal in competing:
                proposal = await transition_memory(
                    self.db,
                    owner_id=owner_id,
                    memory_id=proposal.id,
                    expected_version=proposal.lifecycle_version,
                    event_type="archived",
                    action_key=f"memory:{proposal.id}:competing-archive:v{proposal.lifecycle_version + 1}",
                    to_status="archived",
                    to_validity_state=proposal.validity_state,
                    reason_code="competing_correction_confirmed",
                    archived_from_status="proposed",
                    archived_reason="同一旧认识已有其他纠正被确认",
                    changed_at=now,
                )

        memory = await transition_memory(
            self.db,
            owner_id=owner_id,
            memory_id=memory.id,
            expected_version=memory.lifecycle_version,
            event_type="confirmed",
            action_key=f"memory:{memory.id}:confirmed:v{memory.lifecycle_version + 1}",
            to_status="confirmed",
            to_validity_state="valid",
            archived_from_status=None,
            archived_reason="",
            changed_at=now,
        )
        memory.last_reinforced_at = now
        await bump_context_generation(self.db, owner_id)
        await flush_uow(self.db)
        return memory

    async def archive(self, owner_id: str, memory_id: int, *, reason: str = "用户归档") -> Memory:
        memory = await self.db.get(Memory, memory_id)
        if not memory or memory.owner_id != owner_id:
            raise LookupError("Memory not found")
        if memory.status in {"archived", "expired", "superseded"}:
            return memory
        now = utc_now()
        memory = await transition_memory(
            self.db,
            owner_id=owner_id,
            memory_id=memory.id,
            expected_version=memory.lifecycle_version,
            event_type="archived",
            action_key=f"memory:{memory.id}:archived:v{memory.lifecycle_version + 1}",
            to_status="archived",
            to_validity_state=memory.validity_state,
            reason_code="manual_archive" if reason == "用户归档" else "archive_requested",
            archived_from_status=memory.status,
            archived_reason=reason,
            changed_at=now,
        )
        await bump_context_generation(self.db, owner_id)
        await flush_uow(self.db)
        return memory

    async def restore(self, owner_id: str, memory_id: int) -> Memory:
        memory = await self.db.get(Memory, memory_id)
        if not memory or memory.owner_id != owner_id:
            raise LookupError("Memory not found")
        if (
            memory.status in {"proposed", "confirmed"}
            and memory.lifecycle_reason_code == "manual_restore"
        ):
            return memory
        if memory.status not in {"archived", "expired"}:
            raise ValueError("Only archived or expired memory can be restored")
        if not memory.restorable:
            raise ValueError("This historical memory cannot be restored directly")
        if memory.superseded_by_id is not None:
            raise ValueError("A superseded memory cannot be restored directly")
        restored_status = (
            memory.archived_from_status
            if memory.archived_from_status in {"proposed", "confirmed"}
            else "confirmed"
        )
        now = utc_now()
        restored_expiry = memory.expires_at
        if restored_expiry is not None and coerce_legacy_utc(restored_expiry) <= now:
            # Restoring an already expired fact is an explicit renewal. A
            # future expiry on a manually archived fact is not rewritten.
            restored_expiry = None
        memory = await transition_memory(
            self.db,
            owner_id=owner_id,
            memory_id=memory.id,
            expected_version=memory.lifecycle_version,
            event_type="restored",
            action_key=f"memory:{memory.id}:restored:v{memory.lifecycle_version + 1}",
            to_status=restored_status,
            to_validity_state=memory.validity_state,
            reason_code="manual_restore",
            expires_at=restored_expiry,
            archived_from_status=None,
            archived_reason="",
            changed_at=now,
        )
        await bump_context_generation(self.db, owner_id)
        await flush_uow(self.db)
        return memory

    async def maintain(
        self,
        owner_id: str,
        *,
        before_mutation: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, int]:
        if self.db.in_nested_transaction():
            raise RuntimeError("memory maintenance cannot coordinate inside a SAVEPOINT")
        provider = get_embedding_provider()
        if provider is not None and _has_pending_session_writes(self.db):
            raise RuntimeError(
                "memory maintenance requires a clean session before an embedding wait"
            )
        now = utc_now()
        expired = 0
        archived = 0
        memories = list((await self.db.execute(select(Memory).where(Memory.owner_id == owner_id))).scalars())
        memory_versions = {memory.id: memory.lifecycle_version for memory in memories}
        plans = list((await self.db.execute(
            select(Plan).where(Plan.owner_id == owner_id).options(selectinload(Plan.stages).selectinload(Stage.tasks))
        )).scalars().unique())

        # Compute every DB mutation first, but do not apply it until embedding
        # work has finished.  This keeps provider waits outside a SQLite write
        # transaction even when a configured provider is remote or slow.
        status_updates: dict[int, tuple[str, str, str]] = {}
        for memory in memories:
            if (
                memory.status == "confirmed"
                and memory.validity_state == "valid"
                and memory.provenance_node_id is not None
                and len(memory.content_hash) == 64
                and len(memory.provenance_digest) == 64
                and memory.expires_at
                and coerce_legacy_utc(memory.expires_at) <= now
            ):
                status_updates[memory.id] = (
                    "expired",
                    "已到期",
                    "expiry_reached",
                )
            elif (
                memory.status == "confirmed"
                and memory.validity_state == "valid"
                and memory.provenance_node_id is not None
                and len(memory.content_hash) == 64
                and len(memory.provenance_digest) == 64
                and memory.layer in {"short_term", "episodic"}
                and coerce_legacy_utc(memory.updated_at) < now - timedelta(days=90)
            ):
                status_updates[memory.id] = (
                    "archived",
                    "短期/情节记忆超过 90 天未更新",
                    "stale_memory",
                )

        existing_plan_ids = {str(plan.id) for plan in plans}
        for memory in memories:
            if (
                memory.status == "confirmed"
                and memory.validity_state == "valid"
                and memory.provenance_node_id is not None
                and len(memory.content_hash) == 64
                and len(memory.provenance_digest) == 64
                and memory.id not in status_updates
                and memory.scope == "plan"
                and memory.scope_id not in existing_plan_ids
            ):
                status_updates[memory.id] = (
                    "archived",
                    "关联计划已不存在",
                    "missing_plan_scope",
                )

        plan_summaries = {plan.id: build_plan_memory_summary(plan) for plan in plans}

        embedding_updates: dict[int, list[float]] = {}
        if provider is not None:
            # Plans/memories above are immutable inputs to the provider phase;
            # end their read snapshot before awaiting any embedding work.
            await commit_uow(self.db)
            for memory in memories:
                effective_status = status_updates.get(
                    memory.id,
                    (memory.status, "", ""),
                )[0]
                if (
                    effective_status == "confirmed"
                    and memory.validity_state == "valid"
                    and memory.provenance_node_id is not None
                    and len(memory.content_hash) == 64
                    and len(memory.provenance_digest) == 64
                    and (
                        memory.embedding is None
                        or memory.embedding_provider != provider.name
                    )
                ):
                    embedding_updates[memory.id] = await asyncio.to_thread(
                        provider.embed,
                        memory.content,
                    )

        if before_mutation is not None:
            # Registry coordination can acquire the RunEvent serialization
            # boundary here: all provider waits are complete, while no ORM
            # mutation or flush has started yet.
            await before_mutation()
        for memory in memories:
            if memory.id in status_updates:
                status, reason, reason_code = status_updates[memory.id]
                try:
                    memory = await transition_memory(
                        self.db,
                        owner_id=owner_id,
                        memory_id=memory.id,
                        expected_version=memory_versions[memory.id],
                        event_type="expired" if status == "expired" else "archived",
                        action_key=(
                            f"memory:{memory.id}:maintain-{status}:"
                            f"v{memory_versions[memory.id] + 1}"
                        ),
                        to_status=status,
                        to_validity_state=memory.validity_state,
                        reason_code=reason_code,
                        archived_from_status="confirmed",
                        archived_reason=reason,
                        changed_at=now,
                    )
                except MemoryLifecycleConflict:
                    continue
                if status == "expired":
                    expired += 1
                else:
                    archived += 1
            if memory.id in embedding_updates:
                expected_version = (
                    memory_versions[memory.id] + 1
                    if memory.id in status_updates
                    and memory.lifecycle_version == memory_versions[memory.id] + 1
                    else memory_versions[memory.id]
                )
                embedding_result = await self.db.execute(
                    update(Memory)
                    .where(
                        Memory.id == memory.id,
                        Memory.owner_id == owner_id,
                        Memory.lifecycle_version == expected_version,
                        Memory.status == "confirmed",
                        Memory.validity_state == "valid",
                        Memory.provenance_node_id.is_not(None),
                        func.length(Memory.content_hash) == 64,
                        func.length(Memory.provenance_digest) == 64,
                    )
                    .values(
                        embedding=embedding_updates[memory.id],
                        embedding_provider=provider.name if provider is not None else None,
                    )
                )
                if embedding_result.rowcount == 1:
                    set_committed_value(memory, "embedding", embedding_updates[memory.id])
                    if provider is not None:
                        set_committed_value(memory, "embedding_provider", provider.name)
        plans_refreshed = 0
        for plan in plans:
            plan_result = await self.db.execute(
                update(Plan)
                .where(
                    Plan.id == plan.id,
                    Plan.owner_id == owner_id,
                    Plan.version == plan.version,
                    or_(
                        Plan.memory_summary.is_(None),
                        Plan.memory_summary != plan_summaries[plan.id],
                    ),
                )
                .values(memory_summary=plan_summaries[plan.id])
            )
            if plan_result.rowcount == 1:
                plans_refreshed += 1
                set_committed_value(plan, "memory_summary", plan_summaries[plan.id])
        if expired or archived or plans_refreshed:
            await bump_context_generation(self.db, owner_id)
        await flush_uow(self.db)
        return {
            "expired": expired,
            "archived": archived,
            "plans_refreshed": plans_refreshed,
        }

    async def compress_session(self, session: Session, client: Any | None = None) -> bool:
        if self.db.in_nested_transaction():
            raise RuntimeError("memory compression cannot coordinate inside a SAVEPOINT")
        if client is not None and _has_pending_session_writes(self.db):
            raise RuntimeError(
                "compress_session requires a clean session before a model wait"
            )
        if self.db.bind is None:
            raise RuntimeError("memory compression requires a bound database session")

        # End a caller-owned read snapshot before the independent claim UoW.
        # Every path, including deterministic offline fallback, uses the same
        # durable claim/cursor protocol.
        if _has_pending_session_writes(self.db):
            raise RuntimeError(
                "compress_session requires a clean session before a durable claim"
            )
        if self.db.in_transaction():
            await commit_uow(self.db)
        compression_sessions = async_sessionmaker(
            self.db.bind,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        claim = await run_short_transaction(
            compression_sessions,
            lambda short_db: _acquire_compression_claim(
                short_db,
                session_id=session.id,
            ),
        )
        if claim is None:
            return False
        chunks, chunk_counts = _compression_chunks(
            claim.source_manifest,
            max_bytes=_compression_chunk_budget(),
        )

        if client is None:
            summary_content = _fallback_summary(
                base_summary=claim.base_summary,
                chunks=chunks,
                source_digest=claim.source_digest,
            )
            method = "fallback"
        else:
            summary_content = claim.base_summary
            method = "model"
            try:
                for chunk_index, chunk in enumerate(chunks, start=1):
                    request_messages = [
                        {
                            "role": "system",
                            "content": _compression_system_prompt(),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Existing verified summary:\n{summary_content or '(empty)'}\n\n"
                                f"Source chunk {chunk_index}/{len(chunks)}:\n{chunk}"
                            ),
                        },
                    ]
                    ensure_request_fits(messages=request_messages, tools=None)
                    response = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=settings.MODEL_NAME,
                            messages=request_messages,
                            temperature=0.2,
                            max_tokens=settings.AGENT_OUTPUT_TOKEN_RESERVE,
                        ),
                        timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                    )
                    next_summary = response.choices[0].message.content or ""
                    if not next_summary.strip():
                        raise RuntimeError("compression model returned an empty chunk summary")
                    summary_content = _truncate_utf8(next_summary.strip(), 12_000)
                    claim_is_live = await run_short_transaction(
                        compression_sessions,
                        lambda short_db: _renew_compression_claim(
                            short_db,
                            claim=claim,
                        ),
                    )
                    if not claim_is_live:
                        return False
            except asyncio.CancelledError:
                await run_short_transaction(
                    compression_sessions,
                    lambda short_db: _release_compression_claim(short_db, claim=claim),
                )
                raise
            except Exception:
                await run_short_transaction(
                    compression_sessions,
                    lambda short_db: _release_compression_claim(short_db, claim=claim),
                )
                return False

        compressed = await run_short_transaction(
            compression_sessions,
            lambda short_db: _finalize_compression(
                short_db,
                claim=claim,
                summary_content=summary_content,
                method=method,
                message_chunk_counts=chunk_counts,
            ),
        )
        if compressed:
            await self.db.refresh(session)
        return compressed


def _normalize_memory(value: str) -> str:
    return " ".join(value.casefold().split())
