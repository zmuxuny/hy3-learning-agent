"""An approval pause should already have a readable, user-owned session title."""
import json
from types import SimpleNamespace
import pytest
from app.db.database import AsyncSessionLocal
from app.models import AgentRun,Session,ToolInvocation
from app.runtime.agent import AgentRuntime
from app.runtime.session_titles import initial_session_title,pending_plan_title
from test_run_recovery import FakeToolCall

@pytest.mark.parametrize('manual',[False,True])
async def test_plan_approval_names_session_without_waiting_for_terminal(monkeypatch,manual):
    import app.tools as module
    objective='我会Python基础语法，希望一周学会pathlib并完成文件统计工具，请先给我可审阅的计划。'
    original='我的手动标题' if manual else initial_session_title(objective)
    async with AsyncSessionLocal() as db:
        session=Session(owner_id='local',title=original);db.add(session);await db.flush()
        run=AgentRun(owner_id='local',session_id=session.id,trigger='user_message',objective=objective,model='hy3');db.add(run);await db.commit();rid,sid=run.id,session.id
    runtime=AgentRuntime();runtime.client=object()
    async def model(*_args,**_kwargs):
        return SimpleNamespace(content=None,tool_calls=[FakeToolCall('proposal-title','plan_proposal_create',json.dumps({'plan':{'title':'一周完成文件统计工具'}},ensure_ascii=False))]),None
    async def tool(name, arguments, context):
        context.db.add(ToolInvocation(owner_id='local',run_id=rid,
            idempotency_key=rid+':proposal-title',tool_name=name,tool_call_id='proposal-title',
            args_hash='0'*64,canonical_args=json.loads(arguments),status='pending_approval'))
        await context.db.commit()
        return {'ok':False,'data':{'approval_required':True,'blocking':True,'reason':'Review the exact proposal'}}
    monkeypatch.setattr(runtime,'_call_model',model);monkeypatch.setattr(module,'execute_tool',tool)
    await runtime.run(rid)
    async with AsyncSessionLocal() as db:
        assert (await db.get(AgentRun,rid)).status=='waiting_approval'
        assert (await db.get(Session,sid)).title==(original if manual else '一周完成文件统计工具')


def test_title_requires_an_actual_plan_proposal_and_supports_json_transport():
    assert pending_plan_title({'name':'plan_proposal_create','arguments':json.dumps({'plan':json.dumps({'title':'“目录统计工具”'})})})=='目录统计工具'
    assert pending_plan_title({'name':'plan_proposal_create','arguments':'invalid'}) is None
    assert pending_plan_title({'name':'file_write','arguments':{'plan':{'title':'外部内容'}}}) is None
