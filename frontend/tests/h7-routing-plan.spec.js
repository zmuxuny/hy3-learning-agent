import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createMemoryHistory, createRouter } from 'vue-router';

import api from '../src/api/client.js';
import appRouter, { routes } from '../src/router.js';
import { useInboxStore } from '../src/stores/inbox.js';
import { usePlanStore } from '../src/stores/plan.js';
import { useSessionStore } from '../src/stores/session.js';
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

describe('H7 route ownership and deep links', () => {
  it.each([
    ['/', 'home', {}],
    ['/sessions/session-7', 'session', { sessionId: 'session-7' }],
    ['/plans', 'plans', {}],
    ['/plans/42', 'plan', { planId: '42' }],
    ['/inbox', 'inbox', {}],
    ['/inbox/intervention-9', 'inbox-intervention', { interventionId: 'intervention-9' }],
    ['/memory', 'memory', {}],
    ['/settings', 'settings', {}],
    ['/archives', 'archives', {}],
  ])('resolves %s as the canonical %s route', async (path, name, params) => {
    const router = createRouter({ history: createMemoryHistory(), routes });
    await router.push(path);
    await router.isReady();

    expect(router.currentRoute.value.name).toBe(name);
    expect(router.currentRoute.value.params).toEqual(params);
  });

  describe('missing deep-link entities', () => {
    beforeEach(() => {
      setActivePinia(createPinia());
      useShellStore().coreReady = true;
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    it('returns an unknown session id to the home route without selecting stale state', async () => {
      const shell = useShellStore();
      const session = useSessionStore();
      session.sessions = [{ id: 'known-session', title: 'Known' }];
      session.activeSessionId = 'known-session';
      const replace = vi.spyOn(appRouter, 'replace').mockResolvedValue();

      await shell.hydrateRoute({
        name: 'session',
        params: { sessionId: 'missing-session' },
        fullPath: '/sessions/missing-session',
      }, { force: true });

      expect(replace).toHaveBeenCalledOnce();
      expect(replace).toHaveBeenCalledWith({ name: 'home' });
      expect(session.activeSessionId).toBe('known-session');
    });

    it('returns an unknown plan id to the plan index and clears stale detail', async () => {
      const shell = useShellStore();
      const plan = usePlanStore();
      plan.plans = [{ id: 1, title: 'Known', status: 'active' }];
      plan.currentPlan = { id: 1, title: 'Stale detail' };
      plan.planResources = [{ id: 3 }];
      const replace = vi.spyOn(appRouter, 'replace').mockResolvedValue();

      await shell.hydrateRoute({
        name: 'plan',
        params: { planId: '404' },
        fullPath: '/plans/404',
      }, { force: true });

      expect(replace).toHaveBeenCalledOnce();
      expect(replace).toHaveBeenCalledWith({ name: 'plans' });
      expect(plan.currentPlan).toBeNull();
      expect(plan.planResources).toEqual([]);
    });

    it('returns an unknown intervention id to the inbox instead of leaving a dead detail URL', async () => {
      const shell = useShellStore();
      const inbox = useInboxStore();
      inbox.notifications = [{ id: 1, intervention_id: 'known-intervention' }];
      const replace = vi.spyOn(appRouter, 'replace').mockResolvedValue();

      await shell.hydrateRoute({
        name: 'inbox-intervention',
        params: { interventionId: 'missing-intervention' },
        fullPath: '/inbox/missing-intervention',
      }, { force: true });

      expect(replace).toHaveBeenCalledOnce();
      expect(replace).toHaveBeenCalledWith({ name: 'inbox' });
    });
  });
});

describe('H7 plan archive state', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('clears active detail and execution focus after archive succeeds', async () => {
    const plan = usePlanStore();
    const active = { id: 7, title: 'TypeScript', status: 'active' };
    const archived = { ...active, status: 'archived' };
    plan.plans = [active];
    plan.currentPlan = active;
    plan.planResources = [{ id: 2, plan_id: 7 }];
    plan.competencyGraph = { nodes: [{ id: 'ts' }] };
    plan.evidenceObservations = [{ id: 3 }];
    plan.focusPlanId = 7;
    vi.spyOn(api, 'patch').mockResolvedValue({ data: archived });
    vi.spyOn(api, 'get').mockImplementation(async (path) => {
      if (path === '/plans') return { data: [] };
      if (path === '/plans?archived=true') return { data: [archived] };
      throw new Error(`Unexpected GET ${path}`);
    });

    const result = await plan.setArchived(7, true);

    expect(api.patch).toHaveBeenCalledOnce();
    expect(api.patch).toHaveBeenCalledWith('/plans/7/archive', { archived: true });
    expect(result).toEqual({ plan: archived, archived: true, detailCleared: true, focusCleared: true });
    expect(plan.plans).toEqual([]);
    expect(plan.archivedPlans).toEqual([archived]);
    expect(plan.currentPlan).toBeNull();
    expect(plan.planResources).toEqual([]);
    expect(plan.competencyGraph).toBeNull();
    expect(plan.evidenceObservations).toEqual([]);
    expect(plan.focusPlanId).toBeNull();
    expect(plan.focusedPlan).toBeNull();
  });

  it('keeps detail and focus unchanged when the archive request fails', async () => {
    const plan = usePlanStore();
    const active = { id: 7, title: 'TypeScript', status: 'active' };
    plan.plans = [active];
    plan.currentPlan = active;
    plan.planResources = [{ id: 2, plan_id: 7 }];
    plan.focusPlanId = 7;
    vi.spyOn(api, 'patch').mockRejectedValue(new Error('archive unavailable'));

    await expect(plan.setArchived(7, true)).rejects.toThrow('archive unavailable');

    expect(api.patch).toHaveBeenCalledOnce();
    expect(api.patch).toHaveBeenCalledWith('/plans/7/archive', { archived: true });
    expect(plan.plans).toEqual([active]);
    expect(plan.archivedPlans).toEqual([]);
    expect(plan.currentPlan).toEqual(active);
    expect(plan.planResources).toEqual([{ id: 2, plan_id: 7 }]);
    expect(plan.focusPlanId).toBe(7);
  });

  it('does not let a late pre-archive refresh re-activate an archived plan', async () => {
    const plan = usePlanStore();
    const active = { id: 7, title: 'TypeScript', status: 'active' };
    const archived = { ...active, status: 'archived' };
    const staleActive = deferred();
    const staleArchive = deferred();
    plan.plans = [active];
    plan.currentPlan = active;
    plan.focusPlanId = 7;
    vi.spyOn(api, 'patch').mockResolvedValue({ data: archived });
    vi.spyOn(api, 'get').mockImplementation((path) => {
      if (path === '/plans') return staleActive.promise;
      if (path === '/plans?archived=true') return staleArchive.promise;
      throw new Error(`Unexpected GET ${path}`);
    });

    await plan.setArchived(7, true);
    expect(plan.plans).toEqual([]);
    expect(plan.archivedPlans).toEqual([archived]);

    staleActive.resolve({ data: [active] });
    staleArchive.resolve({ data: [] });
    await settle();

    expect(api.get.mock.calls).toEqual([
      ['/plans'],
      ['/plans?archived=true'],
    ]);
    expect(plan.plans).toEqual([]);
    expect(plan.archivedPlans).toEqual([archived]);
    expect(plan.focusPlanId).toBeNull();
  });
});
