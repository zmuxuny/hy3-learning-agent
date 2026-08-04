import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models import AgentRun, ChatMessage
from app.runtime.agent import AgentRuntime


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)

    def model_dump(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


def questions_call():
    return FakeToolCall(
        "call-intake-questions",
        "planning_intake_update",
        json.dumps({
            "goal": "三个月掌握 Java 并交付一个项目",
            "confirmed_facts": [{"key": "基础", "value": "零基础"}],
            "open_questions": [
                {
                    "id": "q-time",
                    "prompt": "每周能投入多少时间？",
                    "why": "决定计划跨度与任务粒度",
                    "options": ["5 小时以内", "5-10 小时", "10 小时以上"],
                    "allow_custom": True,
                },
                {
                    "id": "q-output",
                    "prompt": "希望最终交付什么？",
                    "why": "用于设计阶段综合考核",
                    "options": [],
                    "allow_custom": True,
                },
            ],
            "readiness": "collecting",
            "readiness_confidence": 0.55,
            "rationale": "还需要确认时间投入和期望产出",
        }, ensure_ascii=False),
    )


def ready_intake_call():
    return FakeToolCall(
        "call-intake-ready",
        "planning_intake_update",
        json.dumps({
            "goal": "三个月掌握 Java 并交付一个项目",
            "confirmed_facts": [
                {"key": "基础", "value": "零基础"},
                {"key": "时间", "value": "每周 8 小时"},
            ],
            "open_questions": [],
            "readiness": "ready",
            "readiness_confidence": 0.95,
            "rationale": "信息已经充分",
        }, ensure_ascii=False),
    )


def proposal_call():
    return FakeToolCall(
        "call-proposal",
        "plan_proposal_create",
        json.dumps({
            "plan": {
                "title": "零基础 Java 实战计划",
                "goal": "三个月掌握 Java 并交付一个项目",
                "current_level": "零基础",
                "weekly_minutes": 480,
                "expected_outcome": "可运行的 Java 项目",
                "stages": [{
                    "title": "第一阶段",
                    "tasks": [{"title": "完成环境搭建", "is_core": True}],
                }],
            },
            "rationale": "基于澄清结果拆分阶段",
        }, ensure_ascii=False),
    )


class CardCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="先澄清需求，再生成提案。",
                reasoning_content=None,
                tool_calls=[questions_call(), ready_intake_call(), proposal_call()],
            ))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="这是完整的计划提案，请审阅。",
            reasoning_content=None,
            tool_calls=None,
        ))])


@pytest.mark.asyncio
async def test_assistant_message_snapshots_planning_cards_into_metadata():
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="帮我制定一份零基础 Java 学习计划",
            model="hy3",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=CardCompletions()))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        message = (await db.execute(
            select(ChatMessage).where(
                ChatMessage.run_id == run_id,
                ChatMessage.role == "assistant",
            )
        )).scalars().one()
        cards = message.message_metadata.get("cards", [])
        assert [card["kind"] for card in cards] == ["planning_questions", "plan_proposal"]

        questions = cards[0]["intake"]
        assert len(questions["open_questions"]) == 2
        assert questions["open_questions"][0]["id"] == "q-time"
        assert questions["source_run_id"] == run_id
        assert questions["readiness"] == "collecting"

        proposal = cards[1]["proposal"]
        assert proposal["title"] == "零基础 Java 实战计划"
        assert proposal["status"] == "pending"
        assert proposal["plan_payload"]["stages"][0]["tasks"][0]["title"] == "完成环境搭建"
        assert proposal["source_run_id"] == run_id


@pytest.mark.asyncio
async def test_upsert_card_keeps_one_snapshot_per_kind():
    from app.runtime.agent import _upsert_card

    cards = []
    _upsert_card(cards, {"kind": "planning_questions", "intake": {"n": 1}})
    _upsert_card(cards, {"kind": "plan_proposal", "proposal": {"title": "A"}})
    _upsert_card(cards, {"kind": "planning_questions", "intake": {"n": 2}})
    assert [card["kind"] for card in cards] == ["planning_questions", "plan_proposal"]
    assert cards[0]["intake"] == {"n": 2}
