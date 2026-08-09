from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models import ChatMessage, Memory, Plan, Session, SessionSummary, Stage
from app.retrieval import get_embedding_provider
from app.retrieval.bm25 import BM25
from app.retrieval.provider import embedding_similarity
from app.retrieval.text import tokenize_terms


LAYER_WEIGHT = {"semantic": 4.0, "long_term": 4.0, "episodic": 2.5, "short_term": 2.0, "working": 1.0}


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
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(Memory).where(Memory.owner_id == owner_id, Memory.status == "confirmed")
        )
        candidates: list[Memory] = []
        for memory in result.scalars():
            if memory.expires_at and _aware(memory.expires_at) <= now:
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

        query_terms = tokenize_terms(query)
        provider = get_embedding_provider()
        query_embedding = provider.embed(query) if (provider is not None and query_terms) else None

        documents = [tokenize_terms(memory.content) for memory in candidates]
        bm25 = BM25()
        bm25.fit(documents)
        bm25_scores = [bm25.score(query_terms, index) for index in range(len(candidates))]

        vector_scores: list[float] = []
        for memory in candidates:
            memory_embedding = memory.embedding
            if memory_embedding is None and provider is not None and query_terms:
                memory_embedding = provider.embed(memory.content)
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
            age_days = max(0, (now - _aware(memory.updated_at)).days)
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

        scored.sort(key=lambda item: (item[0], _aware(item[1].updated_at)), reverse=True)
        ranked = scored[:limit]
        for _, memory, _ in ranked:
            memory.last_accessed_at = now
            memory.access_count = (memory.access_count or 0) + 1
        await self.db.flush()
        return [item[1] for item in ranked], [item[2] for item in ranked]

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
                now = datetime.now(timezone.utc)
                existing.confidence = max(existing.confidence, confidence)
                existing.last_reinforced_at = now
                existing.updated_at = now
                await self.db.flush()
                return existing, True

        if supersedes_id is not None:
            previous = await self.db.get(Memory, supersedes_id)
            if not previous or previous.owner_id != owner_id:
                raise ValueError("Superseded memory not found")
            if previous.status not in {"confirmed", "proposed"}:
                raise ValueError("Only active memory can be corrected")
            if previous.scope != scope or previous.scope_id != scope_id:
                raise ValueError("A correction must keep the original memory scope")

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
        await self.db.flush()
        return memory, False

    async def confirm(self, owner_id: str, memory_id: int) -> Memory:
        memory = await self.db.get(Memory, memory_id)
        if not memory or memory.owner_id != owner_id:
            raise LookupError("Memory not found")
        if memory.status == "confirmed":
            now = datetime.now(timezone.utc)
            memory.last_reinforced_at = now
            memory.updated_at = now
            await self.db.flush()
            return memory
        if memory.status != "proposed":
            raise ValueError("Only proposed memory can be confirmed")

        now = datetime.now(timezone.utc)
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
        await self.db.flush()
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
        memory.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
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
        memory.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return memory

    async def maintain(self, owner_id: str) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        expired = 0
        archived = 0
        memories = list((await self.db.execute(select(Memory).where(Memory.owner_id == owner_id))).scalars())
        for memory in memories:
            if memory.status == "confirmed" and memory.expires_at and _aware(memory.expires_at) <= now:
                memory.archived_from_status = "confirmed"
                memory.archived_reason = "已到期"
                memory.status = "expired"
                memory.updated_at = now
                expired += 1
            elif (
                memory.status == "confirmed"
                and memory.layer in {"short_term", "episodic"}
                and _aware(memory.updated_at) < now - timedelta(days=90)
            ):
                memory.archived_from_status = "confirmed"
                memory.archived_reason = "短期/情节记忆超过 90 天未更新"
                memory.status = "archived"
                memory.updated_at = now
                archived += 1

        plans = list((await self.db.execute(
            select(Plan).where(Plan.owner_id == owner_id).options(selectinload(Plan.stages).selectinload(Stage.tasks))
        )).scalars().unique())
        existing_plan_ids = {str(plan.id) for plan in plans}
        for memory in memories:
            if (
                memory.status == "confirmed"
                and memory.scope == "plan"
                and memory.scope_id not in existing_plan_ids
            ):
                memory.archived_from_status = "confirmed"
                memory.archived_reason = "关联计划已不存在"
                memory.status = "archived"
                memory.updated_at = now
                archived += 1
        for plan in plans:
            tasks = [task for stage in plan.stages for task in stage.tasks]
            completed = [task for task in tasks if task.status == "completed"]
            blocked = [task.title for task in tasks if task.status == "blocked"]
            active = [task.title for task in tasks if task.status == "active"]
            next_pending = next((task.title for task in tasks if task.status == "pending"), "")
            plan.memory_summary = (
                f"进度 {len(completed)}/{len(tasks)}；"
                f"当前任务：{'、'.join(active[:3]) or next_pending or '无'}；"
                f"阻塞：{'、'.join(blocked[:3]) or '无'}；"
                f"计划版本 {plan.version}。"
            )

        provider = get_embedding_provider()
        if provider is not None:
            for memory in memories:
                if memory.status == "confirmed" and (
                    memory.embedding is None or memory.embedding_provider != provider.name
                ):
                    memory.embedding = provider.embed(memory.content)
                    memory.embedding_provider = provider.name
        await self.db.flush()
        return {"expired": expired, "archived": archived, "plans_refreshed": len(plans)}

    async def compress_session(self, session: Session, client: Any | None = None) -> bool:
        result = await self.db.execute(
            select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at, ChatMessage.id)
        )
        messages = [
            message for message in result.scalars()
            if not message.message_metadata.get("superseded_by_edit")
        ]
        keep = settings.AGENT_RECENT_MESSAGE_LIMIT
        if len(messages) <= settings.AGENT_SESSION_COMPRESSION_THRESHOLD:
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
        await self.db.flush()
        return True


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _normalize_memory(value: str) -> str:
    return " ".join(value.casefold().split())
