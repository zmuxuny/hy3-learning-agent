import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import api from '../src/api/client.js';
import router from '../src/router.js';
import { useInboxStore } from '../src/stores/inbox.js';
import { usePlanStore } from '../src/stores/plan.js';
import { useRunStore } from '../src/stores/run.js';
import { useSessionStore } from '../src/stores/session.js';
import { useSettingsStore } from '../src/stores/settings.js';
import { useShellStore } from '../src/stores/shell.js';

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
}

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    FakeEventSource.instances.push(this);
  }

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }

  close() {
    this.closed = true;
  }
}
FakeEventSource.instances = [];

describe('H7 durable intervention reply target', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('maps the open response top-level intervention to the queue top-level target', async () => {
    const inbox = useInboxStore();
    const session = useSessionStore();
    const shell = useShellStore();
    const notification = {
      id: 31,
      title: '复习提醒',
      body: '请回复',
      session_id: 'session-1',
      intervention_id: 'stale-nested-value',
    };
    const queued = {
      id: 51,
      objective: '我已经复习了',
      session_id: 'session-1',
      position: 10,
      version: 1,
      reply_to_intervention_id: 'intervention-31',
    };
    session.activeSessionId = 'session-1';
    vi.spyOn(api, 'post').mockImplementation(async (path, body) => {
      if (path === '/notifications/31/open') {
        expect(body).toBeUndefined();
        return {
          data: {
            notification,
            intervention_id: 'intervention-31',
            session_id: 'session-1',
            message_id: 80,
          },
        };
      }
      if (path === '/agent/queue') {
        expect(body).toEqual({
          objective: '我已经复习了',
          session_id: 'session-1',
          plan_id: null,
          reply_to_intervention_id: 'intervention-31',
        });
        return { data: queued };
      }
      throw new Error(`Unexpected POST ${path}`);
    });
    vi.spyOn(api, 'get').mockImplementation(async (path) => {
      if (path === '/agent/queue?session_id=session-1') return { data: [queued] };
      throw new Error(`Unexpected GET ${path}`);
    });

    await inbox.openNotification(31);
    expect(inbox.replyTargetNotification.intervention_id).toBe('intervention-31');

    await shell.enqueueMessage('我已经复习了');

    expect(api.post.mock.calls).toEqual([
      ['/notifications/31/open'],
      ['/agent/queue', {
        objective: '我已经复习了',
        session_id: 'session-1',
        plan_id: null,
        reply_to_intervention_id: 'intervention-31',
      }],
    ]);
    expect(session.queuedMessages).toEqual([queued]);
    expect(session.queuedMessages[0].reply_to_intervention_id).toBe('intervention-31');
    expect(inbox.replyTargetNotification).toBeNull();
  });

  it('preserves the reply target when queue creation fails', async () => {
    const inbox = useInboxStore();
    const shell = useShellStore();
    inbox.replyTargetNotification = {
      id: 31,
      intervention_id: 'intervention-31',
    };
    vi.spyOn(api, 'post').mockRejectedValue(new Error('queue unavailable'));

    await expect(shell.enqueueMessage('稍后再答')).rejects.toThrow('queue unavailable');

    expect(api.post).toHaveBeenCalledOnce();
    expect(api.post).toHaveBeenCalledWith('/agent/queue', {
      objective: '稍后再答',
      session_id: null,
      plan_id: null,
      reply_to_intervention_id: 'intervention-31',
    });
    expect(inbox.replyTargetNotification.intervention_id).toBe('intervention-31');
  });

  it('keeps the durable target through reload, edit, reorder and dispatch, then leaves a normal message clean', async () => {
    const inbox = useInboxStore();
    const session = useSessionStore();
    const shell = useShellStore();
    const plan = usePlanStore();
    const targetItem = {
      id: 51,
      objective: 'edited reply',
      session_id: 'session-1',
      position: 10,
      version: 2,
      reply_to_intervention_id: 'intervention-31',
    };
    const ordinaryItem = {
      id: 52,
      objective: 'ordinary follow-up',
      session_id: 'session-1',
      position: 20,
      version: 1,
      reply_to_intervention_id: null,
    };
    const reorderedTarget = { ...targetItem, position: 20, version: 3 };
    const reorderedOrdinary = { ...ordinaryItem, position: 10, version: 2 };
    const run = { id: 'run-51', session_id: 'session-1', plan_id: 7, status: 'queued' };
    let queueSnapshot = [targetItem, ordinaryItem];
    session.activeSessionId = 'session-1';
    plan.plans = [{ id: 7, status: 'active', title: 'Plan' }];
    const push = vi.spyOn(router, 'push').mockResolvedValue();
    vi.spyOn(api, 'get').mockImplementation(async (path) => {
      if (path === '/agent/queue?session_id=session-1') return { data: queueSnapshot };
      if (path === '/agent/sessions') return { data: [{ id: 'session-1', plan_id: 7, last_run_id: 'run-51' }] };
      throw new Error(`Unexpected GET ${path}`);
    });
    vi.spyOn(api, 'patch').mockImplementation(async (path, body) => {
      if (path === '/agent/queue/51' && body.objective === 'edited reply') {
        expect(body).toEqual({ objective: 'edited reply', expected_version: 2 });
        queueSnapshot = [targetItem, ordinaryItem];
        return { data: targetItem };
      }
      if (path === '/agent/queue/51' && body.position === 20) {
        expect(body).toEqual({ position: 20, expected_version: 2 });
        queueSnapshot = [reorderedOrdinary, reorderedTarget];
        return { data: reorderedTarget };
      }
      throw new Error(`Unexpected PATCH ${path} ${JSON.stringify(body)}`);
    });
    vi.spyOn(api, 'post').mockImplementation(async (path, body) => {
      if (path === '/agent/queue/51/send') {
        expect(body).toEqual({ expected_version: 3 });
        queueSnapshot = [reorderedOrdinary];
        return { data: run };
      }
      if (path === '/agent/queue') {
        expect(body).toEqual({
          objective: 'next ordinary message',
          session_id: 'session-1',
          plan_id: 7,
          reply_to_intervention_id: null,
        });
        const created = { ...ordinaryItem, id: 53, objective: body.objective };
        queueSnapshot = [reorderedOrdinary, created];
        return { data: created };
      }
      throw new Error(`Unexpected POST ${path}`);
    });

    await session.loadQueue();
    expect(session.queuedMessages[0].reply_to_intervention_id).toBe('intervention-31');
    await session.updateQueuedMessage(51, { objective: 'edited reply' });
    expect(session.queuedMessages[0].reply_to_intervention_id).toBe('intervention-31');
    await session.moveQueuedMessage(51, 1);
    expect(session.queuedMessages[1].reply_to_intervention_id).toBe('intervention-31');
    await shell.sendQueuedMessage(51);

    expect(api.post).toHaveBeenCalledWith('/agent/queue/51/send', { expected_version: 3 });
    expect(push).toHaveBeenCalledWith({ name: 'session', params: { sessionId: 'session-1' } });
    expect(FakeEventSource.instances[0].url).toBe('/api/v1/agent/runs/run-51/events/stream');

    await shell.enqueueMessage('next ordinary message');
    expect(api.post).toHaveBeenLastCalledWith('/agent/queue', {
      objective: 'next ordinary message',
      session_id: 'session-1',
      plan_id: 7,
      reply_to_intervention_id: null,
    });
    expect(inbox.replyTargetNotification).toBeNull();
  });
});

describe('H7 independent core and optional bootstrap', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('makes core usable while optional email and dashboard reject or never settle', async () => {
    const shell = useShellStore();
    const settings = useSettingsStore();
    const plan = usePlanStore();
    const session = useSessionStore();
    const never = new Promise(() => {});
    const calls = [];
    vi.spyOn(api, 'get').mockImplementation((path) => {
      calls.push(path);
      if (path === '/profile') return Promise.resolve({ data: { id: 1, display_name: 'Learner' } });
      if (path === '/plans') return Promise.resolve({ data: [{ id: 7, status: 'active', title: 'Plan' }] });
      if (path === '/agent/sessions') return Promise.resolve({ data: [{ id: 'session-1', title: 'Core' }] });
      if (path === '/agent/runs') return Promise.resolve({ data: [] });
      if (path === '/agent/queue') return Promise.resolve({ data: [] });
      if (path === '/settings') return never;
      return Promise.reject(new Error(`optional unavailable: ${path}`));
    });

    await shell.bootstrap();

    expect(shell.coreReady).toBe(true);
    expect(shell.coreLoading).toBe(false);
    expect(settings.profile).toEqual({ id: 1, display_name: 'Learner' });
    expect(plan.plans).toEqual([{ id: 7, status: 'active', title: 'Plan' }]);
    expect(session.sessions).toEqual([{ id: 'session-1', title: 'Core' }]);
    expect(calls).toEqual(expect.arrayContaining([
      '/profile', '/plans', '/agent/sessions', '/agent/runs', '/agent/queue',
      '/dashboard', '/settings/email', '/settings', '/settings/proactive', '/settings/followup',
    ]));
  });

  it('retains last-known-good optional data when a later optional refresh fails', async () => {
    const shell = useShellStore();
    const settings = useSettingsStore();
    settings.dashboard = { activity: [{ id: 'known' }], achievements: [], due_review_count: 2, open_quiz_count: 1 };
    settings.emailConfiguration = { smtp_configured: true, smtp_username: 'masked-user' };
    settings.appSettings = { model: 'known-model' };
    vi.spyOn(api, 'get').mockImplementation((path) => {
      if (path === '/profile') return Promise.resolve({ data: { id: 1 } });
      if (path === '/plans' || path === '/agent/sessions' || path === '/agent/runs' || path === '/agent/queue') {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`optional refresh failed: ${path}`));
    });

    await shell.bootstrap();
    await settle();

    expect(shell.coreReady).toBe(true);
    expect(settings.dashboard).toEqual({ activity: [{ id: 'known' }], achievements: [], due_review_count: 2, open_quiz_count: 1 });
    expect(settings.emailConfiguration).toEqual({ smtp_configured: true, smtp_username: 'masked-user' });
    expect(settings.appSettings).toEqual({ model: 'known-model' });
  });

  it('ignores late responses from an older bootstrap generation', async () => {
    const shell = useShellStore();
    const settings = useSettingsStore();
    const oldProfile = deferred();
    const oldDashboard = deferred();
    const counts = new Map();
    vi.spyOn(api, 'get').mockImplementation((path) => {
      const count = (counts.get(path) || 0) + 1;
      counts.set(path, count);
      if (path === '/profile') {
        return count === 1 ? oldProfile.promise : Promise.resolve({ data: { id: 2, display_name: 'new' } });
      }
      if (path === '/dashboard') {
        return count === 1 ? oldDashboard.promise : Promise.resolve({ data: { activity: [{ id: 'new' }] } });
      }
      if (path === '/plans' || path === '/agent/sessions' || path === '/agent/runs' || path === '/agent/queue') {
        return Promise.resolve({ data: [] });
      }
      return Promise.resolve({ data: path.includes('archived=true') ? [] : {} });
    });

    const first = shell.bootstrap();
    const second = shell.bootstrap();
    await second;
    oldProfile.resolve({ data: { id: 1, display_name: 'old' } });
    oldDashboard.resolve({ data: { activity: [{ id: 'old' }] } });
    await first;
    await settle();

    expect(settings.profile).toEqual({ id: 2, display_name: 'new' });
    expect(settings.dashboard).toEqual({ activity: [{ id: 'new' }] });
    expect(shell.coreReady).toBe(true);
    expect(shell.coreLoading).toBe(false);
  });
});
