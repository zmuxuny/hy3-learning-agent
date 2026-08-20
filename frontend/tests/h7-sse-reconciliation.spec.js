import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import api from '../src/api/client.js';
import { useRunStore } from '../src/stores/run.js';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    this.closed = false;
    FakeEventSource.instances.push(this);
  }

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }

  emit(name, data) {
    this.listeners.get(name)?.({ data: JSON.stringify(data) });
  }

  fail() {
    this.onerror?.(new Event('error'));
  }

  close() {
    this.closed = true;
  }
}
FakeEventSource.instances = [];

function event(sequence, type, summary = type) {
  return {
    sequence,
    event_type: type,
    summary,
    payload: {},
    created_at: `2026-08-21T00:00:${String(sequence).padStart(2, '0')}Z`,
  };
}

describe('H7 SSE reconciliation controller', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('coalesces an error burst into one REST event reconciliation and merges by sequence', async () => {
    const runStore = useRunStore();
    const eventsResponse = deferred();
    const paths = [];
    runStore.currentRun = { id: 'run-1', session_id: 'session-1', status: 'running' };
    runStore.runEvents = [{
      sequence: 1,
      type: 'run.started',
      summary: 'started',
      payload: {},
      created_at: '2026-08-21T00:00:01Z',
    }];
    vi.spyOn(api, 'get').mockImplementation((path) => {
      paths.push(path);
      if (path === '/agent/runs/run-1/events') return eventsResponse.promise;
      if (path === '/agent/runs/run-1') {
        return Promise.resolve({ data: { id: 'run-1', session_id: 'session-1', status: 'running' } });
      }
      if (path === '/agent/sessions/session-1/messages') return Promise.resolve({ data: [] });
      throw new Error(`Unexpected GET ${path}`);
    });

    runStore.subscribeToRun('run-1', false);
    const source = FakeEventSource.instances[0];
    source.fail();
    source.fail();
    source.fail();
    await settle();

    expect(paths.filter((path) => path === '/agent/runs/run-1/events')).toHaveLength(1);

    eventsResponse.resolve({
      data: [
        event(3, 'assistant.status', 'third'),
        event(2, 'context.built', 'second'),
        event(2, 'context.built', 'duplicate second'),
        event(1, 'run.started', 'duplicate first'),
      ],
    });
    await settle();

    expect(runStore.runEvents.map((item) => item.sequence)).toEqual([1, 2, 3]);
    expect(runStore.runEvents.find((item) => item.sequence === 2).summary).toBe('second');
  });

  it('stops after a terminal event and does not reconnect from a stale error callback', async () => {
    vi.useFakeTimers();
    const runStore = useRunStore();
    const onTerminal = vi.fn().mockResolvedValue();
    runStore.currentRun = { id: 'run-1', session_id: 'session-1', status: 'running' };
    runStore.setEventHandlers({ onTerminal });
    vi.spyOn(api, 'get').mockRejectedValue(new Error('must not reconcile after terminal'));
    runStore.subscribeToRun('run-1');
    const source = FakeEventSource.instances[0];

    source.emit('run.completed', {
      sequence: 8,
      type: 'run.completed',
      summary: 'done',
      payload: {},
      created_at: '2026-08-21T00:00:08Z',
    });
    await settle();

    expect(source.closed).toBe(true);
    expect(runStore.currentRun.status).toBe('completed');
    expect(onTerminal).toHaveBeenCalledOnce();

    source.fail();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(api.get).not.toHaveBeenCalled();
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('fences both stale stream events and late reconciliation when the selected run changes', async () => {
    const runStore = useRunStore();
    const oldEvents = deferred();
    vi.spyOn(api, 'get').mockImplementation((path) => {
      if (path === '/agent/runs/run-1/events') return oldEvents.promise;
      if (path === '/agent/runs/run-1') {
        return Promise.resolve({ data: { id: 'run-1', session_id: 'session-1', status: 'running' } });
      }
      if (path === '/agent/sessions/session-1/messages') return Promise.resolve({ data: [] });
      throw new Error(`Unexpected GET ${path}`);
    });
    runStore.currentRun = { id: 'run-1', session_id: 'session-1', status: 'running' };
    runStore.subscribeToRun('run-1');
    const oldSource = FakeEventSource.instances[0];
    oldSource.fail();
    await settle();

    runStore.currentRun = { id: 'run-2', session_id: 'session-2', status: 'running' };
    runStore.subscribeToRun('run-2');
    const currentSource = FakeEventSource.instances[1];
    expect(oldSource.closed).toBe(true);

    oldSource.emit('assistant.status', {
      sequence: 90,
      type: 'assistant.status',
      summary: 'stale stream event',
      payload: {},
    });
    currentSource.emit('assistant.status', {
      sequence: 2,
      type: 'assistant.status',
      summary: 'current event',
      payload: {},
    });
    oldEvents.resolve({ data: [event(91, 'assistant.message', 'late stale reconciliation')] });
    await settle();

    expect(runStore.runEvents.map((item) => item.summary)).toEqual(['current event']);
    expect(runStore.runEventCache['run-1']?.map((item) => item.summary) || []).not.toContain('late stale reconciliation');
  });
});
