<script setup>
import { CheckCircleIcon, ClockIcon, ShieldCheckIcon } from '@heroicons/vue/24/outline';
import { useWorkspaceStore } from '../stores/workspace';
import AgentComposer from './AgentComposer.vue';

const store = useWorkspaceStore();
</script>

<template>
  <section class="view">
    <header class="view-header compact-header">
      <div><span class="eyebrow">PLANS & EVIDENCE</span><h1>学习计划</h1><p>Agent 可以读取、协调和修改计划，所有写操作都有审计与撤销。</p></div>
    </header>

    <div class="plans-layout">
      <aside class="plan-picker panel">
        <button v-for="plan in store.plans" :key="plan.id" :class="{ active: store.currentPlan?.id === plan.id }" @click="store.selectPlan(plan.id)">
          <span class="plan-dot"></span><div><strong>{{ plan.title }}</strong><small>{{ plan.status }} · v{{ plan.version }}</small></div>
        </button>
        <div v-if="!store.plans.length" class="empty-state compact">还没有计划</div>
      </aside>

      <main v-if="store.currentPlan" class="plan-detail">
        <article class="panel plan-hero">
          <div><span class="eyebrow">PLAN {{ store.currentPlan.id }} · VERSION {{ store.currentPlan.version }}</span><h2>{{ store.currentPlan.title }}</h2><p>{{ store.currentPlan.goal }}</p></div>
          <div class="plan-score"><strong>{{ Math.round(store.currentPlan.progress * 100) }}%</strong><span>整体进度</span></div>
        </article>

        <div class="kanban">
          <article v-for="stage in store.currentPlan.stages" :key="stage.id" class="stage-column">
            <header><div><small>阶段 {{ stage.position + 1 }}</small><h3>{{ stage.title }}</h3></div><span>{{ stage.tasks.length }}</span></header>
            <div class="stage-tasks">
              <div v-for="task in stage.tasks" :key="task.id" class="task-card">
                <div class="task-title"><component :is="task.status === 'completed' ? CheckCircleIcon : ClockIcon" /><strong>{{ task.title }}</strong></div>
                <p>{{ task.description || `${task.estimated_minutes} 分钟学习任务` }}</p>
                <footer><span>{{ task.kind }}</span><span v-if="task.is_core" class="core-tag"><ShieldCheckIcon /> 需证据</span></footer>
              </div>
            </div>
          </article>
        </div>
      </main>
      <main v-else class="panel empty-state">选择一个计划，或让 Agent 创建完整计划。</main>
    </div>
    <AgentComposer />
  </section>
</template>
