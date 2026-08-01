from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models import ChatMessage, LearningEvent, Memory, Plan, Session, Stage


LAYER_WEIGHT = {"semantic": 4.0, "long_term": 4.0, "episodic": 2.5, "short_term": 2.0, "working": 1.0}


def search_terms(text: str) -> set[str]:
    normalized = text.lower()
    words = set(re.findall(r"[a-z0-9_+#.-]{2,}|[\u4e00-\u9fff]", normalized))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    words.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    return {word for word in words if word}


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
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(Memory).where(Memory.owner_id == owner_id, Memory.status == "confirmed")
        )
        query_terms = search_terms(query)
        ranked: list[tuple[float, Memory]] = []
        for memory in result.scalars():
            if memory.expires_at and _aware(memory.expires_at) <= now:
                continue
            scope_match = memory.scope == "global" or (
                plan_id is not None and memory.scope == "plan" and memory.scope_id == str(plan_id)
            ) or (
                session_id is not None and memory.scope == "session" and memory.scope_id == session_id
            )
            if not scope_match:
                continue
            overlap = len(query_terms.intersection(search_terms(memory.content)))
            age_days = max(0, (now - _aware(memory.updated_at)).days)
            recency = max(0.0, 2.0 - age_days / 30)
            scope_bonus = 3.0 if memory.scope in {"plan", "session"} else 1.0
            score = overlap * 3.0 + LAYER_WEIGHT.get(memory.layer, 1.0) + recency + scope_bonus + memory.confidence
            ranked.append((score, memory))
        ranked.sort(key=lambda item: (item[0], _aware(item[1].updated_at)), reverse=True)
        return [item[1] for item in ranked[:limit]]

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
            plan.memory_summary = (
                f"进度 {len(completed)}/{len(tasks)}；"
                f"当前任务：{'、'.join(active[:3]) or '无'}；"
                f"阻塞：{'、'.join(blocked[:3]) or '无'}；"
                f"计划版本 {plan.version}。"
            )
        await self.db.flush()
        return {"expired": expired, "archived": archived, "plans_refreshed": len(plans)}

    async def compress_session(self, session: Session, client: Any | None = None) -> bool:
        result = await self.db.execute(
            select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at, ChatMessage.id)
        )
        messages = list(result.scalars())
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
