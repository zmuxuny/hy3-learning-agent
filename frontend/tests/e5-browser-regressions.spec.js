import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';

import api from '../src/api/client.js';
import UserMessage from '../src/components/UserMessage.vue';
import RunDisclosure from '../src/components/RunDisclosure.vue';
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
});
