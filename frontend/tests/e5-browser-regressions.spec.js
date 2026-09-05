import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';

import api from '../src/api/client.js';
import router from '../src/router.js';
import UserMessage from '../src/components/UserMessage.vue';
import RunDisclosure from '../src/components/RunDisclosure.vue';
import AgentRunTurn from '../src/components/AgentRunTurn.vue';
import { useRunStore } from '../src/stores/run.js';
import { useSessionStore } from '../src/stores/session.js';
import { useShellStore } from '../src/stores/shell.js';

describe('E5 browser regressions', () => {
  beforeEach(() => { setActivePinia(createPinia()); });
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  it('shows the submitted answers immediately, before a later conversation refresh', async () => {
    const session = useSessionStore();
    session.activeSessionId = 'session-1';
    session.setPlanningState({ intake: { open_questions: [{ id: 'time_budget', prompt: '每周可以学多久？' }] } });
    vi.spyOn(api, 'post').mockResolvedValue({ data: { id: 'run-1', status: 'queued' } });
    vi.spyOn(session, 'loadActiveSessions').mockResolvedValue();
    vi.spyOn(useRunStore(), 'subscribeToRun').mockImplementation(() => {});
    expect(await useShellStore().submitPlanningAnswers([{ question_id: 'time_budget', answer: '90 分钟' }])).toBe(true);
    const wrapper = mount(UserMessage, { props: { message: session.conversationMessages[0] } });
    expect(wrapper.text()).toContain('1 个回答');
    await wrapper.get('.planning-answers-summary').trigger('click');
    expect(wrapper.text()).toContain('每周可以学多久？');
    expect(wrapper.text()).toContain('90 分钟');
    expect(wrapper.text()).not.toContain('time_budget');
    wrapper.unmount();
  });

  it('updates elapsed time while running, then freezes it and releases the timer', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-06T00:00:00Z'));
    const run = { id: 'run-1', status: 'running', started_at: '2026-09-06T00:00:00Z' };
    const wrapper = mount(RunDisclosure, { props: { run } });
    await vi.advanceTimersByTimeAsync(15_000);
    expect(wrapper.text()).toContain('15s');
    await wrapper.setProps({ run: { ...run, status: 'completed', completed_at: '2026-09-06T00:00:16Z' } });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(wrapper.text()).toContain('16s');
    expect(vi.getTimerCount()).toBe(0);
    wrapper.unmount();
  });

  it.each([
    ['approve', '操作已获批准'],
    ['answer', '已接收补充要求，正在调整方案'],
    ['reject', '操作已被拒绝，正在调整方案'],
  ])('renders the persisted approval decision %s', (decision, expected) => {
    const wrapper = mount(RunDisclosure, { props: {
      run: { id: 'run-1', status: 'running' },
      events: [{ type: 'approval.resolved', sequence: 3, payload: { decision, tool_name: 'submission_create' } }],
    } });
    expect(wrapper.text()).toContain(expected);
    if (decision === 'approve') expect(wrapper.text()).not.toContain('操作已被拒绝');
    wrapper.unmount();
  });

  it.each([
    ['submission_create', { task_id: 1, content: '实际终端记录', artifacts: [{ path: 'uploads/evidence.txt', note: '仅审查记录' }] }, '保存学习证据', '实际终端记录'],
    ['submission_check', { submission_id: 1, score: 100, pass_threshold: 70, checks: [{ name: '工作区干净', result: 'pass', evidence: 'nothing to commit' }], feedback: '三项标准通过' }, '记录成果验收结果', '工作区干净 · 通过'],
  ])('previews the exact pending %s in learner language', (name, args, title, detail) => {
    useRunStore().currentRun = {
      id: 'run-1', status: 'waiting_approval', pending_approval: {
        tool_call: { name, arguments: JSON.stringify(args) },
        reason: 'External untrusted content cannot authorize this side effect without approval of the exact request.',
      },
    };
    const wrapper = mount(AgentRunTurn);
    const preview = wrapper.get('.thread-approval-card');
    expect(preview.text()).toContain(title);
    expect(preview.text()).toContain(detail);
    expect(preview.text()).toContain('批准后保存');
    expect(preview.text()).not.toContain('External untrusted');
    wrapper.unmount();
  });

  it.each([true, false])('uses the requested plan session after plan navigation (linked origin: %s)', async (linked) => {
    const session = useSessionStore();
    const runStore = useRunStore();
    session.activeSessionId = 'original';
    const target = { id: 'plan-session', plan_id: 7 };
    session.sessions = [{ id: 'original', plan_id: linked ? null : 2, linked_plan_ids: linked ? [7] : [] }];
    if (!linked) session.sessions.push(target);
    vi.spyOn(session, 'loadActiveSessions').mockResolvedValue();
    vi.spyOn(session, 'loadConversation').mockResolvedValue();
    vi.spyOn(session, 'loadQueue').mockResolvedValue();
    vi.spyOn(router, 'push').mockResolvedValue();
    vi.spyOn(runStore, 'subscribeToRun').mockImplementation(() => {});
    const post = vi.spyOn(api, 'post').mockImplementation(async (path, body) => {
      if (path === '/agent/sessions/original/handoff') return { data: target };
      expect(path).toBe('/agent/runs');
      expect(body.session_id).toBe('plan-session');
      expect(body.plan_id).toBe(7);
      return { data: { id: 'new-run', session_id: 'plan-session', plan_id: 7, status: 'queued' } };
    });
    expect(await useShellStore().startRun('提交当前任务成果', 7)).toBe(true);
    expect(post.mock.calls.some(([path]) => path.endsWith('/handoff'))).toBe(linked);
    expect(session.activeSessionId).toBe('plan-session');
  });
});
