from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update
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
from app.models import ChatMessage, Memory, Plan, Session, SessionSummary, Stage
from app.retrieval import get_embedding_provider
from app.retrieval.bm25 import BM25
from app.retrieval.provider import embedding_similarity
from app.retrieval.text import tokenize_terms


LAYER_WEIGHT = {"semantic": 4.0, "long_term": 4.0, "episodic": 2.5, "short_term": 2.0, "working": 1.0}
_SESSION_WRITE_ACTIVITY_KEY = "h2_session_write_activity"


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
        now = utc_now()
        query_terms = tokenize_terms(query)
        provider = get_embedding_provider()
        if provider is not None and query_terms and _has_pending_session_writes(self.db):
            raise RuntimeError(
                "memory retrieval requires a clean session before an embedding wait"
            )
        result = await self.db.execute(
            select(Memory).where(Memory.owner_id == owner_id, Memory.status == "confirmed")
        )
        candidates: list[Memory] = []
        for memory in result.scalars():
            if memory.expires_at and coerce_legacy_utc(memory.expires_at) <= now:
                continue
            scope_match = memory.scope == "global" or (
                plan_id is not None and memory.scope == "plan" and memory.scope_id == str(plan_id)
            ) or (
                session_id is not None and memory.scope == "session" and memory.scope_id == session_id
            )
            if scope_match:
                candidates.append(memory)

        if not candidates:
            return [], []

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

        rank_lists: list[list[int]] = []
        if max(bm25_scores, default=0.0) > 0:
            rank_lists.append(_rank_map(bm25_scores))
        if max(vector_scores, default=0.0) > 0:
            rank_lists.append(_rank_map(vector_scores))

        scored: list[tuple[float, Memory, dict[str, Any]]] = []
        for index, memory in enumerate(candidates):
            ranks = [rank_list[index] for rank_list in rank_lists]
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
                "rrf": round(rrf, 4),
                "hybrid": round(hybrid, 4),
                "scope": scope_bonus,
                "layer": layer_weight,
                "recency": round(recency, 4),
                "confidence": memory.confidence,
                "total": round(total, 4),
            }
            scored.append((total, memory, breakdown))

        scored.sort(
            key=lambda item: (item[0], coerce_legacy_utc(item[1].updated_at)),
            reverse=True,
        )
        ranked = scored[:limit]
        ranked_memories = [item[1] for item in ranked]
        if ranked_memories and self.db.bind is not None:
            memory_ids = [memory.id for memory in ranked_memories]
            access_sessions = async_sessionmaker(
                self.db.bind,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            async def record_access(short_db: AsyncSession) -> None:
                await short_db.execute(
                    update(Memory)
                    .where(Memory.id.in_(memory_ids))
                    .values(
                        last_accessed_at=now,
                        access_count=Memory.access_count + 1,
                    )
                )

            try:
                await run_short_transaction(access_sessions, record_access)
            except DatabaseBusyError:
                # Access telemetry is not a domain fact and must never make a
                # read unavailable. A later retrieval can update it again.
                pass
            else:
                for memory in ranked_memories:
                    set_committed_value(memory, "last_accessed_at", now)
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
            )
        )
        for existing in result.scalars():
            if existing.scope_id == scope_id and _normalize_memory(existing.content) == normalized:
                now = utc_now()
                existing.confidence = max(existing.confidence, confidence)
                existing.last_reinforced_at = now
                existing.updated_at = now
                await flush_uow(self.db)
                return existing, True

        if supersedes_id is not None:
            previous = await self.db.get(Memory, supersedes_id)
            if not previous or previous.owner_id != owner_id:
                raise ValueError("Superseded memory not found")
            if previous.status not in {"confirmed", "proposed"}:
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
        )
        self.db.add(memory)
        await flush_uow(self.db)
        return memory, False

    async def confirm(self, owner_id: str, memory_id: int) -> Memory:
        memory = await self.db.get(Memory, memory_id)
        if not memory or memory.owner_id != owner_id:
            raise LookupError("Memory not found")
        if memory.status == "confirmed":
            now = utc_now()
            memory.last_reinforced_at = now
            memory.updated_at = now
            await flush_uow(self.db)
            return memory
        if memory.status != "proposed":
            raise ValueError("Only proposed memory can be confirmed")

        now = utc_now()
        if memory.supersedes_id is not None:
            previous = await self.db.get(Memory, memory.supersedes_id)
            if not previous or previous.owner_id != owner_id:
                raise ValueError("Superseded memory not found")
            if previous.status not in {"confirmed", "proposed"}:
                raise ValueError("The original memory changed before this correction was confirmed")
            if previous.scope != memory.scope or previous.scope_id != memory.scope_id:
                raise ValueError("A correction must keep the original memory scope")
            previous_status = previous.status
            previous.status = "superseded"
            previous.archived_from_status = previous_status
            previous.archived_reason = "由用户确认的新认识替代"
            previous.superseded_by_id = memory.id
            previous.updated_at = now
            competing = list((await self.db.execute(
                select(Memory).where(
                    Memory.owner_id == owner_id,
                    Memory.supersedes_id == previous.id,
                    Memory.status == "proposed",
                    Memory.id != memory.id,
                )
            )).scalars())
            for proposal in competing:
                proposal.archived_from_status = "proposed"
                proposal.status = "archived"
                proposal.archived_reason = "同一旧认识已有其他纠正被确认"
                proposal.updated_at = now

        memory.status = "confirmed"
        memory.archived_from_status = None
        memory.archived_reason = ""
        memory.last_reinforced_at = now
        memory.updated_at = now
        await flush_uow(self.db)
        return memory

    async def archive(self, owner_id: str, memory_id: int, *, reason: str = "用户归档") -> Memory:
        memory = await self.db.get(Memory, memory_id)
        if not memory or memory.owner_id != owner_id:
            raise LookupError("Memory not found")
        if memory.status in {"archived", "expired", "superseded"}:
            return memory
        memory.archived_from_status = memory.status
        memory.status = "archived"
        memory.archived_reason = reason
        memory.updated_at = utc_now()
        await flush_uow(self.db)
        return memory

    async def restore(self, owner_id: str, memory_id: int) -> Memory:
        memory = await self.db.get(Memory, memory_id)
        if not memory or memory.owner_id != owner_id:
            raise LookupError("Memory not found")
        if memory.status not in {"archived", "expired"}:
            raise ValueError("Only archived or expired memory can be restored")
        if not memory.restorable:
            raise ValueError("This historical memory cannot be restored directly")
        if memory.superseded_by_id is not None:
            raise ValueError("A superseded memory cannot be restored directly")
        memory.status = (
            memory.archived_from_status
            if memory.archived_from_status in {"proposed", "confirmed"}
            else "confirmed"
        )
        memory.archived_from_status = None
        memory.archived_reason = ""
        memory.expires_at = None
        memory.updated_at = utc_now()
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
        plans = list((await self.db.execute(
            select(Plan).where(Plan.owner_id == owner_id).options(selectinload(Plan.stages).selectinload(Stage.tasks))
        )).scalars().unique())

        # Compute every DB mutation first, but do not apply it until embedding
        # work has finished.  This keeps provider waits outside a SQLite write
        # transaction even when a configured provider is remote or slow.
        status_updates: dict[int, tuple[str, str]] = {}
        for memory in memories:
            if (
                memory.status == "confirmed"
                and memory.expires_at
                and coerce_legacy_utc(memory.expires_at) <= now
            ):
                status_updates[memory.id] = ("expired", "已到期")
                expired += 1
            elif (
                memory.status == "confirmed"
                and memory.layer in {"short_term", "episodic"}
                and coerce_legacy_utc(memory.updated_at) < now - timedelta(days=90)
            ):
                status_updates[memory.id] = (
                    "archived",
                    "短期/情节记忆超过 90 天未更新",
                )
                archived += 1

        existing_plan_ids = {str(plan.id) for plan in plans}
        for memory in memories:
            if (
                memory.status == "confirmed"
                and memory.id not in status_updates
                and memory.scope == "plan"
                and memory.scope_id not in existing_plan_ids
            ):
                status_updates[memory.id] = ("archived", "关联计划已不存在")
                archived += 1

        plan_summaries: dict[int, str] = {}
        for plan in plans:
            tasks = [task for stage in plan.stages for task in stage.tasks]
            completed = [task for task in tasks if task.status == "completed"]
            blocked = [task.title for task in tasks if task.status == "blocked"]
            active = [task.title for task in tasks if task.status == "active"]
            next_pending = next((task.title for task in tasks if task.status == "pending"), "")
            plan_summaries[plan.id] = (
                f"进度 {len(completed)}/{len(tasks)}；"
                f"当前任务：{'、'.join(active[:3]) or next_pending or '无'}；"
                f"阻塞：{'、'.join(blocked[:3]) or '无'}；"
                f"计划版本 {plan.version}。"
            )

        embedding_updates: dict[int, list[float]] = {}
        if provider is not None:
            # Plans/memories above are immutable inputs to the provider phase;
            # end their read snapshot before awaiting any embedding work.
            await commit_uow(self.db)
            for memory in memories:
                effective_status = status_updates.get(
                    memory.id,
                    (memory.status, ""),
                )[0]
                if effective_status == "confirmed" and (
                    memory.embedding is None or memory.embedding_provider != provider.name
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
                status, reason = status_updates[memory.id]
                memory.archived_from_status = "confirmed"
                memory.archived_reason = reason
                memory.status = status
                memory.updated_at = now
            if memory.id in embedding_updates:
                memory.embedding = embedding_updates[memory.id]
                if provider is not None:  # narrowed above; retained for type checkers
                    memory.embedding_provider = provider.name
        for plan in plans:
            plan.memory_summary = plan_summaries[plan.id]
        await flush_uow(self.db)
        return {"expired": expired, "archived": archived, "plans_refreshed": len(plans)}

    async def compress_session(self, session: Session, client: Any | None = None) -> bool:
        if self.db.in_nested_transaction():
            raise RuntimeError("memory compression cannot coordinate inside a SAVEPOINT")
        if client is not None and _has_pending_session_writes(self.db):
            raise RuntimeError(
                "compress_session requires a clean session before a model wait"
            )
        result = await self.db.execute(
            select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at, ChatMessage.id)
        )
        messages = [
            message for message in result.scalars()
            if not message.message_metadata.get("superseded_by_edit")
        ]
        keep = settings.AGENT_RECENT_MESSAGE_LIMIT
        # The snapshot includes only `keep` recent messages. Compress as soon
        # as history exceeds that boundary so no middle turns disappear.
        threshold = min(settings.AGENT_SESSION_COMPRESSION_THRESHOLD, keep)
        if len(messages) <= threshold:
            return False
        older = messages[:-keep]
        uncompressed = [message for message in older if not message.message_metadata.get("included_in_summary")]
        if not uncompressed:
            return False
        transcript = "\n".join(
            [f"Existing summary: {session.summary}"]
            + [f"{message.role}: {message.content}" for message in uncompressed]
        )
        summary = ""
        method = "model"
        if client is not None:
            # Transcript collection is read-only and the entry guard proved
            # there is no caller-owned work. End that snapshot before waiting
            # for the model; summary mutations begin only after it returns.
            await commit_uow(self.db)
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=settings.MODEL_NAME,
                        messages=[
                            {"role": "system", "content": "Compress the learning conversation into concise Chinese factual memory. Preserve goals, decisions, plan/task IDs, evidence, unresolved blockers, preferences, and commitments. Do not invent facts."},
                            {"role": "user", "content": transcript[-30000:]},
                        ],
                        temperature=0.2,
                    ),
                    timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                )
                summary = response.choices[0].message.content or ""
            except Exception:
                summary = ""
        if not summary:
            method = "fallback"
            excerpts = [f"{message.role}: {' '.join(message.content.split())[:240]}" for message in uncompressed[-12:]]
            summary = "\n".join(part for part in [session.summary, "历史对话压缩：", *excerpts] if part)
        session.summary = summary[:12000]
        summaries = list((await self.db.execute(
            select(SessionSummary).where(SessionSummary.session_id == session.id)
        )).scalars())
        self.db.add(SessionSummary(
            owner_id=session.owner_id,
            session_id=session.id,
            version=max((item.version for item in summaries), default=0) + 1,
            content=session.summary,
            covered_through_message_id=uncompressed[-1].id if uncompressed else None,
            source_message_ids=[message.id for message in uncompressed],
            method=method,
        ))
        for message in uncompressed:
            message.message_metadata = {**message.message_metadata, "included_in_summary": True}
        await flush_uow(self.db)
        return True


def _normalize_memory(value: str) -> str:
    return " ".join(value.casefold().split())
