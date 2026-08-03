<script setup>
import { ArrowRightIcon, CalendarDaysIcon, CheckCircleIcon, ChevronRightIcon, MapIcon } from '@heroicons/vue/24/outline';
import { computed } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const plan = computed(() => {
  const created = store.createdPlanFromCurrentRun;
  if (!created) return null;
  return [...store.plans, ...store.archivedPlans].find((item) => Number(item.id) === Number(created.id)) || created;
});
const taskCount = computed(() => (
  plan.value?.stages?.reduce((count, stage) => count + (stage.tasks?.length || 0), 0) ?? 0
));
</script>

<template>
  <section v-if="plan" class="plan-inline-card">
    <header>
      <span class="plan-inline-icon"><MapIcon /></span>
      <div>
        <small>PLAN CREATED · 计划已建立</small>
        <strong>{{ plan.title }}</strong>
      </div>
      <em>{{ Math.round((plan.progress || 0) * 100) }}%</em>
    </header>
    <p class="plan-inline-goal">{{ plan.goal || '当前仍是全局对话；你可以在计划中继续，建立带交接摘要的计划对话。' }}</p>
    <div class="plan-inline-facts">
      <span><CheckCircleIcon /> {{ plan.stages?.length || 0 }} 个阶段 · {{ taskCount }} 个任务</span>
      <span v-if="plan.deadline"><CalendarDaysIcon /> {{ new Date(plan.deadline).toLocaleDateString('zh-CN') }} 截止</span>
    </div>
    <footer>
      <div>
        <button class="secondary-button" @click="store.selectPlan(plan.id)">打开计划 <ChevronRightIcon /></button>
        <button class="primary-button" @click="store.continueInPlan(plan.id)">在计划中继续 <ArrowRightIcon /></button>
      </div>
    </footer>
  </section>
</template>
