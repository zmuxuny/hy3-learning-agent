import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { defineComponent, nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import AgentMessage from '../src/components/AgentMessage.vue';
import AgentRunTurn from '../src/components/AgentRunTurn.vue';
import AgentTrace from '../src/components/AgentTrace.vue';
import HomeView from '../src/components/HomeView.vue';
import RunDisclosure from '../src/components/RunDisclosure.vue';
import RunTraceButton from '../src/components/RunTraceButton.vue';
import { useRunStore } from '../src/stores/run.js';
import { useSessionStore } from '../src/stores/session.js';
import { useShellStore } from '../src/stores/shell.js';

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    FakeEventSource.instances.push(this);
  }

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }

  emit(name, data) {
    this.listeners.get(name)?.({ data: JSON.stringify(data) });
  }

  close() {
    this.closed = true;
  }
}
FakeEventSource.instances = [];

describe('H7 message protocol rendering', () => {
  it('renders rich markdown while keeping untrusted HTML inert and links isolated', () => {
    const longUrl = `https://example.test/learning/${'long-segment-'.repeat(18)}`;
    const wrapper = mount(AgentMessage, {
      props: {
        content: [
          `See ${longUrl}`,
          '',
          '| Topic | Result |',
          '| --- | --- |',
          '| Context | passed |',
          '',
          '- parent',
          '  - nested child',
          '',
          '```js',
          'const trusted = false;',
          '```',
          '',
          '<img src=x onerror="window.__h7Injected = true">',
        ].join('\n'),
      },
    });

    const link = wrapper.get('a');
    expect(link.attributes('href')).toBe(longUrl);
    expect(link.attributes('target')).toBe('_blank');
    expect(link.attributes('rel')).toContain('noopener');
    expect(wrapper.find('table').exists()).toBe(true);
    expect(wrapper.findAll('ul')).toHaveLength(2);
    expect(wrapper.get('pre code').text()).toContain('const trusted = false;');
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.text()).toContain('<img src=x onerror="window.__h7Injected = true">');
    expect(window.__h7Injected).toBeUndefined();
  });
});

describe('H7 chronological conversation projection', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
    vi.stubGlobal('requestAnimationFrame', (callback) => {
      callback();
      return 1;
    });
    vi.stubGlobal('cancelAnimationFrame', () => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('renders original request, both same-text steers, then the answer', async () => {
    const runStore = useRunStore();
    const sessionStore = useSessionStore();
    runStore.currentRun = { id: 'run-1', objective: 'Original', status: 'completed' };
    runStore.runs = [runStore.currentRun];
    sessionStore.conversationMessages = [
      { id: 1, run_id: 'run-1', role: 'user', content: '原始请求', message_metadata: {} },
      { id: 2, run_id: 'run-1', role: 'user', content: '同文转向', message_metadata: { ui_kind: 'steer', steer_id: 'steer-a' } },
      { id: 3, run_id: 'run-1', role: 'user', content: '同文转向', message_metadata: { ui_kind: 'steer', steer_id: 'steer-b' } },
      { id: 4, run_id: 'run-1', role: 'assistant', content: '最终回答', message_metadata: {} },
    ];

    const wrapper = mount(HomeView);
    await nextTick();

    const projected = wrapper.find('.thread').text();
    expect(projected.match(/同文转向/g)).toHaveLength(2);
    expect(projected.indexOf('原始请求')).toBeLessThan(projected.indexOf('同文转向'));
    expect(projected.lastIndexOf('同文转向')).toBeLessThan(projected.indexOf('最终回答'));
  });

  it('deduplicates steer events by durable identity rather than text', async () => {
    const shell = useShellStore();
    const runStore = useRunStore();
    const sessionStore = useSessionStore();
    sessionStore.activeSessionId = 'session-1';
    shell.configureRunEvents();
    runStore.subscribeToRun('run-1');
    const source = FakeEventSource.instances[0];
    const event = (sequence, steerId) => ({
      sequence,
      type: 'steer.received',
      summary: '同文转向',
      payload: { content: '同文转向', steer_id: steerId },
      created_at: `2026-08-21T00:00:0${sequence}Z`,
    });

    source.emit('steer.received', event(2, 'steer-a'));
    source.emit('steer.received', event(3, 'steer-b'));
    source.emit('steer.received', event(4, 'steer-a'));
    await nextTick();

    expect(sessionStore.conversationMessages.map((message) => ({
      id: message.id,
      content: message.content,
      steerId: message.message_metadata.steer_id,
    }))).toEqual([
      { id: 'steer-steer-a', content: '同文转向', steerId: 'steer-a' },
      { id: 'steer-steer-b', content: '同文转向', steerId: 'steer-b' },
    ]);
  });
});

describe('H7 trace accessibility', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.stubGlobal('requestAnimationFrame', (callback) => {
      callback();
      return 1;
    });
    vi.stubGlobal('cancelAnimationFrame', () => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('renders approval decisions as native focusable buttons and dispatches the exact choice', async () => {
    const runStore = useRunStore();
    const run = {
      id: 'run-approval',
      objective: 'Apply a reviewed change',
      status: 'waiting_approval',
      pending_approval: { reason: 'This change needs confirmation.' },
    };
    runStore.currentRun = run;
    runStore.runEvents = [{
      sequence: 1,
      type: 'approval.required',
      summary: 'Waiting for approval',
      payload: { blocking: true, tool_name: 'plan_patch', reason: 'Review the plan update.' },
    }];
    const decide = vi.spyOn(runStore, 'decideRunApproval').mockResolvedValue(true);
    const wrapper = mount(AgentRunTurn, { props: { run }, attachTo: document.body });
    await nextTick();

    const actions = wrapper.findAll('.approval-actions button');
    expect(actions).toHaveLength(2);
    expect(actions.every((item) => item.element.tagName === 'BUTTON')).toBe(true);
    const approve = wrapper.get('.approval-actions .primary-button');
    approve.element.focus();
    expect(document.activeElement).toBe(approve.element);
    await approve.trigger('click');
    expect(decide).toHaveBeenCalledWith('run-approval', true);
    wrapper.unmount();
  });

  it('uses buttons only for event rows that can actually expand', async () => {
    const runStore = useRunStore();
    const run = { id: 'run-1', objective: 'Review', status: 'failed', created_at: '2026-08-21T00:00:00Z' };
    const events = [
      { sequence: 1, type: 'assistant.status', summary: 'Reading', payload: {} },
      {
        sequence: 2,
        type: 'tool.completed',
        summary: 'Search complete',
        payload: { tool_call_id: 'tool-1', result: { ok: true } },
      },
    ];
    runStore.currentRun = run;
    runStore.runEvents = events;
    const disclosure = mount(RunDisclosure, { props: { run, events } });
    await nextTick();

    const rows = disclosure.findAll('.run-action-line');
    expect(rows).toHaveLength(2);
    expect(rows[0].element.tagName).toBe('DIV');
    expect(rows[0].attributes('aria-expanded')).toBeUndefined();
    expect(rows[1].element.tagName).toBe('BUTTON');
    expect(rows[1].attributes('aria-expanded')).toBe('false');

    const trace = mount(AgentTrace);
    const traceRows = trace.findAll('.trace-event');
    expect(traceRows[0].element.tagName).toBe('DIV');
    expect(traceRows[1].element.tagName).toBe('BUTTON');
  });

  it('opens a labelled modal dialog, closes with Escape and restores focus', async () => {
    const shell = useShellStore();
    const Host = defineComponent({
      components: { AgentTrace, RunTraceButton },
      setup: () => ({ shell }),
      template: '<div><RunTraceButton /><AgentTrace v-if="shell.traceOpen" /></div>',
    });
    const wrapper = mount(Host, { attachTo: document.body });
    const opener = wrapper.get('.trace-toggle');
    opener.element.focus();
    expect(document.activeElement).toBe(opener.element);

    await opener.trigger('click');
    await nextTick();
    const dialog = wrapper.get('[role="dialog"]');
    expect(dialog.attributes('aria-modal')).toBe('true');
    expect(dialog.attributes('aria-labelledby')).toBe('trace-dialog-title');
    expect(dialog.get('#trace-dialog-title').text()).toBe('处理记录');
    expect(dialog.element.contains(document.activeElement)).toBe(true);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await nextTick();

    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    expect(document.activeElement).toBe(opener.element);
    wrapper.unmount();
  });
});
