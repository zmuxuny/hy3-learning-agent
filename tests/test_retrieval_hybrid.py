from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.context.memory import MemoryManager, search_terms
from app.db.database import AsyncSessionLocal
from app.db.uow import commit as commit_uow
from app.models import AgentRun, Memory, Plan
from app.retrieval.bm25 import BM25
from app.retrieval.simhash import simhash
from app.retrieval.text import tokenize_terms
from app.schemas import MemoryRead


async def _memory(
    db,
    owner_id: str,
    content: str,
    *,
    scope: str = "global",
    scope_id: str | None = None,
    layer: str = "semantic",
    confidence: float = 0.9,
    expires_at: datetime | None = None,
) -> Memory:
    memory, reused = await MemoryManager(db).propose(
        owner_id,
        scope=scope,
        scope_id=scope_id,
        layer=layer,
        content=content,
        source_type="user",
        confidence=confidence,
        expires_at=expires_at,
    )
    assert reused is False
    return await MemoryManager(db).confirm(owner_id, memory.id)


@pytest.mark.asyncio
async def test_hybrid_retrieval_ranks_semantic_overlap_with_breakdown():
    async with AsyncSessionLocal() as db:
        await _memory(
            db,
            "local",
            "用户更喜欢通过编写 Python 异步服务来学习 asyncio 并发与超时处理",
        )
        await _memory(db, "local", "用户在准备 Django 模板和数据库模型练习")
        await _memory(db, "local", "用户对前端布局和响应式设计感兴趣")
        await db.commit()

        manager = MemoryManager(db)
        memories, breakdowns = await manager.retrieve_with_scores(
            "local", plan_id=None, query="Python asyncio 并发抓取器超时怎么办", limit=3,
        )

        assert memories
        assert memories[0].content.startswith("用户更喜欢通过编写 Python 异步服务")
        assert len(breakdowns) == len(memories)
        first = breakdowns[0]
        for key in ("memory_id", "bm25", "vector", "rrf", "hybrid", "scope", "layer", "recency", "confidence", "total"):
            assert key in first
        assert first["memory_id"] == memories[0].id
        assert first["hybrid"] > 0
        assert memories[0].access_count == 1
        assert memories[0].last_accessed_at is not None


@pytest.mark.asyncio
async def test_memory_proposals_deduplicate_reinforce_and_preserve_correction_lineage():
    async with AsyncSessionLocal() as db:
        repeat_run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="Reinforce a durable preference",
            status="completed",
            phase="terminal",
        )
        correction_run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="Propose a competing correction",
            status="completed",
            phase="terminal",
        )
        db.add_all([repeat_run, correction_run])
        await db.flush()
        manager = MemoryManager(db)
        original, reused = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="用户喜欢通过项目实战学习 Python",
            source_type="user",
            confidence=0.8,
        )
        assert reused is False
        await manager.confirm("local", original.id)
        duplicate, reused = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="  用户喜欢通过项目实战学习 Python  ",
            source_type="agent_run",
            source_id=repeat_run.id,
            confidence=0.95,
        )
        assert reused is True
        assert duplicate.id == original.id
        assert duplicate.confidence == 0.95
        assert duplicate.last_reinforced_at is not None

        correction, reused = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="用户希望先读原理，再通过项目验证 Python 知识",
            source_type="user",
            confidence=1,
            supersedes_id=original.id,
        )
        assert reused is False
        competing, reused = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="用户只希望阅读 Python 理论，不做项目",
            source_type="agent_run",
            source_id=correction_run.id,
            confidence=0.7,
            supersedes_id=original.id,
        )
        assert reused is False
        await manager.confirm("local", correction.id)
        with pytest.raises(ValueError, match="Only proposed memory"):
            await manager.confirm("local", competing.id)
        await db.commit()

        await db.refresh(original)
        await db.refresh(correction)
        assert original.status == "superseded"
        assert original.superseded_by_id == correction.id
        assert correction.status == "confirmed"
        assert correction.supersedes_id == original.id
        assert competing.status == "archived"
        assert "其他纠正" in competing.archived_reason


@pytest.mark.asyncio
async def test_memory_archive_is_recoverable_without_deleting_history():
    async with AsyncSessionLocal() as db:
        memory = await _memory(db, "local", "每周日进行一次学习复盘")
        expired_memory = await _memory(
            db,
            "local",
            "本周临时复习提醒",
            layer="short_term",
            expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        await db.commit()
        manager = MemoryManager(db)

        archived = await manager.archive("local", memory.id)
        assert archived.status == "archived"
        assert archived.restorable is True
        assert MemoryRead.model_validate(archived).restorable is True
        assert archived.archived_from_status == "confirmed"
        restored = await manager.restore("local", memory.id)
        assert restored.status == "confirmed"
        assert restored.archived_reason == ""
        await commit_uow(db)
        await manager.maintain("local")
        assert expired_memory.status == "expired"
        assert expired_memory.restorable is True
        restored_expired = await manager.restore("local", expired_memory.id)
        assert restored_expired.status == "confirmed"
        assert restored_expired.expires_at is None


@pytest.mark.asyncio
async def test_retrieval_access_tracking_does_not_refresh_fact_freshness():
    async with AsyncSessionLocal() as db:
        semantic_updated_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
        memory = await _memory(db, "local", "用户倾向先理解原理再动手实践")
        memory.updated_at = semantic_updated_at
        await db.commit()

        await MemoryManager(db).retrieve_with_scores(
            "local", plan_id=None, query="学习原理和实践偏好", limit=3,
        )
        await db.commit()
        await db.refresh(memory)

        assert memory.updated_at.replace(tzinfo=timezone.utc) == semantic_updated_at
        assert memory.access_count == 1
        assert memory.last_accessed_at is not None


@pytest.mark.asyncio
async def test_retrieval_respects_plan_isolation():
    async with AsyncSessionLocal() as db:
        first_plan = Plan(owner_id="local", title="Asyncio isolation")
        second_plan = Plan(owner_id="local", title="Django isolation")
        db.add_all([first_plan, second_plan])
        await db.flush()
        await _memory(
            db,
            "local",
            "asyncio 任务超时与并发抓取器改造",
            scope="plan",
            scope_id=str(first_plan.id),
        )
        await _memory(
            db,
            "local",
            "Django 模板继承与数据库模型",
            scope="plan",
            scope_id=str(second_plan.id),
        )
        await db.commit()

        manager = MemoryManager(db)
        plan_a = await manager.retrieve(
            "local", plan_id=first_plan.id, query="并发抓取器超时"
        )
        plan_b = await manager.retrieve(
            "local", plan_id=second_plan.id, query="Django 模板数据库模型"
        )

        assert [item.scope_id for item in plan_a] == [str(first_plan.id)]
        assert [item.scope_id for item in plan_b] == [str(second_plan.id)]


@pytest.mark.asyncio
async def test_retrieval_falls_back_to_bonus_ranking_without_query_terms():
    async with AsyncSessionLocal() as db:
        await _memory(
            db,
            "local",
            "全局长期稳定偏好：编程学习",
            layer="long_term",
            confidence=0.95,
        )
        await _memory(
            db,
            "local",
            "短期临时记录",
            layer="short_term",
            confidence=0.5,
        )
        await db.commit()

        manager = MemoryManager(db)
        memories, breakdowns = await manager.retrieve_with_scores(
            "local", plan_id=None, query="???", limit=5,
        )

        assert len(memories) == 2
        assert memories[0].content.startswith("全局长期稳定偏好")
        assert all(item["hybrid"] == 0 for item in breakdowns)


@pytest.mark.asyncio
async def test_maintain_persists_local_embeddings():
    async with AsyncSessionLocal() as db:
        memory = await _memory(db, "local", "每周末用两小时做一次阶段自测")
        await db.commit()
        memory_id = memory.id

        result = await MemoryManager(db).maintain("local")
        await db.commit()

        assert result["plans_refreshed"] >= 0
        refreshed = await db.get(Memory, memory_id)
        assert refreshed.embedding is not None
        assert len(refreshed.embedding) == 64
        assert refreshed.embedding_provider == "local_hash"


def test_bm25_and_simhash_are_deterministic():
    tokens_a = tokenize_terms("asyncio 并发抓取器超时处理")
    tokens_b = tokenize_terms("asyncio 并发抓取器超时处理")
    tokens_c = tokenize_terms("Django 模板数据库")
    assert tokens_a == tokens_b
    assert simhash(tokens_a) == simhash(tokens_b)
    assert simhash(tokens_a) != simhash(tokens_c)

    bm25 = BM25()
    bm25.fit([tokens_a, tokens_c])
    assert bm25.score(tokens_a, 0) > bm25.score(tokens_a, 1)


def test_search_terms_still_supports_existing_callers():
    terms = search_terms("Python asyncio 并发")
    assert "asyncio" in terms
    assert "并发" in terms
    assert "并发" in terms or "并发" in "".join(terms)
