import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createMemoryHistory, createRouter } from 'vue-router';
import { mount } from '@vue/test-utils';

import api from '../src/api/client.js';
import PlansView from '../src/components/PlansView.vue';
import { usePlanStore } from '../src/stores/plan.js';

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function planFixture() {
  return {
    id: 7,
    title: '数据库事务',
    goal: '能解释并验证原子提交',
    status: 'active',
    progress: 0.5,
    version: 3,
    deadline: null,
    weekly_minutes: 120,
    expected_outcome: '可复现的事务实验',
    stages: [{
      id: 11,
      position: 0,
      title: '核心概念',
      description: '先理解，再验证。',
      status: 'active',
      tasks: [{
        id: 21,
        title: '编写回滚实验',
        description: '验证全部提交或全部回滚。',
        status: 'active',
        kind: 'project',
        estimated_minutes: 45,
        due_at: null,
        review_due_at: null,
        is_core: true,
        evidence_required: true,
        resource_url: null,
      }],
    }],
  };
}

const graphFixture = {
  plan_id: 7,
  revision: 9,
  competencies: [{
    id: 31,
    key: 'transaction-atomicity',
    title: '事务原子性',
    description: '识别部分提交并设计回滚边界。',
    type: 'concept',
    scope: 'plan',
    plan_id: 7,
    status: 'active',
    version: 1,
  }],
  edges: [],
  plan_links: [],
  task_links: [
    { id: 41, task_id: 21, competency_id: 31, relation: 'teaches', target_stage: 'practicing' },
    { id: 42, task_id: 21, competency_id: 31, relation: 'assesses', target_stage: 'demonstrated' },
  ],
  resource_links: [],
};

const evidenceFixture = [{
  id: 51,
  source_type: 'submission_checked',
  source_id: 'submission-51',
  run_id: 'run-51',
  session_id: 'session-7',
  plan_id: 7,
  task_id: 21,
  competency_refs: [{
    competency_id: 31,
    competency_key: 'transaction-atomicity',
    association_kind: 'task_assesses',
    task_competency_link_id_snapshot: 42,
  }],
  fact_kind: 'observation',
  evidence_role: 'primary',
  eligibility_stage: 'demonstrated',
  eligibility_reason: 'VERIFIED_INDEPENDENT_RESULT',
  counts_as_success: true,
  outcome: 'passed',
  occurred_at: '2026-08-21T01:00:00Z',
}];

describe('H7 M14 read-only learning map', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('loads graph and evidence from their strict plan endpoints without coupling optional failures', async () => {
    const plan = usePlanStore();
    vi.spyOn(api, 'get').mockImplementation(async (path) => {
      if (path === '/plans/7') return { data: planFixture() };
      if (path === '/plans/7/resources') throw new Error('optional resources unavailable');
      if (path === '/plans/7/competencies') return { data: graphFixture };
      if (path === '/plans/7/evidence-observations') return { data: { observations: evidenceFixture } };
      throw new Error(`Unexpected GET ${path}`);
    });

    await plan.loadPlan(7);
    await settle();

    expect(plan.currentPlan.id).toBe(7);
    expect(plan.competencyGraph).toEqual(graphFixture);
    expect(plan.evidenceObservations).toEqual(evidenceFixture);
    expect(plan.errors.resources).toBe('optional resources unavailable');
    expect(plan.errors.competencies).toBeUndefined();
    expect(plan.errors.evidence).toBeUndefined();
    expect(api.get).toHaveBeenCalledTimes(4);
    expect(api.get.mock.calls.map(([path]) => path)).toEqual([
      '/plans/7',
      '/plans/7/resources',
      '/plans/7/competencies',
      '/plans/7/evidence-observations',
    ]);
  });

  it('answers trains, proves and evidence-source questions without inferring mastery', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const plan = usePlanStore();
    plan.currentPlan = planFixture();
    plan.competencyGraph = graphFixture;
    plan.evidenceObservations = evidenceFixture;

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/plans/:planId', name: 'plan', component: PlansView }],
    });
    await router.push('/plans/7');
    await router.isReady();
    const wrapper = mount(PlansView, {
      global: {
        plugins: [pinia, router],
        stubs: {
          AgentComposer: true,
          RunTraceButton: true,
        },
      },
    });

    const map = wrapper.get('.learning-map-section');
    expect(map.text()).toContain('只读 · 图版本 9');
    expect(map.text()).toContain('事务原子性');
    expect(map.text()).toContain('训练 · 编写回滚实验');
    expect(map.text()).toContain('证明 · 编写回滚实验');
    expect(map.text()).toContain('成果验收 · passed');
    expect(map.text()).toContain('已证明');
    expect(map.text()).toContain('计入成功证据');
    expect(map.text()).toContain('不把完成进度推断成掌握度');
    expect(map.text()).not.toContain('掌握率');
  });
});
