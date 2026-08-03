from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models import ChatMessage, LearningEvent, Memory, Plan, Session, Stage
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
        return [item[1] for item in ranked], [item[2] for item in ranked]

    async def maintain(self, owner_id: str) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        expired = 0
        archived = 0
        memories = list((await self.db.execute(select(Memory).where(Memory.owner_id == owner_id))).scalars())
        for memory in memories:
            if memory.status == "confirmed" and memory.expires_at and _aware(memory.expires_at) <= now:
                memory.status = "expired"
                expired += 1
            elif (
                memory.status == "confirmed"
                and memory.layer in {"short_term", "episodic"}
                and _aware(memory.updated_at) < now - timedelta(days=90)
            ):
                memory.status = "archived"
                archived += 1

        plans = list((await self.db.execute(
            select(Plan).where(Plan.owner_id == owner_id).options(selectinload(Plan.stages).selectinload(Stage.tasks))
        )).scalars().unique())
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
            excerpts = [f"{message.role}: {' '.join(message.content.split())[:240]}" for message in uncompressed[-12:]]
            summary = "\n".join(part for part in [session.summary, "历史对话压缩：", *excerpts] if part)
        session.summary = summary[:12000]
        for message in uncompressed:
            message.message_metadata = {**message.message_metadata, "included_in_summary": True}
        await self.db.flush()
        return True


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
