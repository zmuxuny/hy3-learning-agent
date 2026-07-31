<script setup>
import { BellAlertIcon, BoltIcon, CalendarDaysIcon, FireIcon, TrophyIcon } from '@heroicons/vue/24/outline';
import { computed } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentComposer from './AgentComposer.vue';

const store = useWorkspaceStore();
const days = computed(() => store.dashboard.activity.map((day, index) => ({
  index,
  date: day.date,
  value: Math.min(day.count, 4),
})));
const completedTasks = computed(() => store.plans.reduce(
  (total, plan) => total + plan.stages.flatMap((stage) => stage.tasks).filter((task) => task.status === 'completed').length,
  0,
));
</script>

<template>
  <section class="view home-view">
    <header class="view-header">
      <div>
        <span class="eyebrow">LEARNING COCKPIT</span>
        <h1>今天，Agent 会和你一起推进什么？</h1>
        <p>它会持续读取计划、记忆和学习事件，在值得介入时主动行动。</p>
      </div>
      <button class="heartbeat-button" @click="store.triggerHeartbeat">
        <BoltIcon /> 立即运行一次心跳
      </button>
    </header>

    <div v-if="store.error" class="error-banner">{{ store.error }}</div>

    <div class="metric-grid">
      <article class="metric-card accent-warm">
        <span><FireIcon /></span>
        <div><strong>{{ store.profile?.streak_days || 0 }}</strong><small>连续学习天数</small></div>
      </article>
      <article class="metric-card">
        <span><CalendarDaysIcon /></span>
        <div><strong>{{ store.activePlans.length }}</strong><small>进行中的计划</small></div>
      </article>
      <article class="metric-card">
        <span><TrophyIcon /></span>
        <div><strong>{{ completedTasks }}</strong><small>已验证任务</small></div>
      </article>
      <article class="metric-card">
        <span><BellAlertIcon /></span>
        <div><strong>{{ store.unreadCount }}</strong><small>待处理主动消息</small></div>
      </article>
    </div>

    <div class="home-grid">
      <article class="panel activity-panel">
        <div class="panel-heading">
          <div><span class="eyebrow">CONSISTENCY</span><h3>学习热力图</h3></div>
          <small>最近 12 周 · 仅记录真实活动</small>
        </div>
        <div class="heatmap">
          <span v-for="day in days" :key="day.index" :class="`heat-${day.value}`" :title="`${day.date}: ${day.value} 次活动`"></span>
        </div>
        <div class="heatmap-legend"><span>少</span><i></i><i></i><i></i><i></i><i></i><span>多</span></div>
      </article>

      <article class="panel coach-panel">
        <div class="coach-orb"><BoltIcon /></div>
        <div>
          <span class="eyebrow">PROACTIVE COACH</span>
          <h3>当前没有需要打扰你的事情</h3>
          <p>后台心跳会记录“保持安静”的决策。主动不等于频繁通知。</p>
        </div>
      </article>
    </div>

    <article class="panel plan-overview">
      <div class="panel-heading">
        <div><span class="eyebrow">ACTIVE PLANS</span><h3>计划进度</h3></div>
        <button class="text-button" @click="store.activeView = 'plans'">查看全部</button>
      </div>
      <div v-if="store.activePlans.length" class="plan-card-grid">
        <button v-for="plan in store.activePlans" :key="plan.id" class="overview-plan" @click="store.selectPlan(plan.id)">
          <div class="progress-ring" :style="{ '--progress': `${Math.round(plan.progress * 360)}deg` }">
            <span>{{ Math.round(plan.progress * 100) }}%</span>
          </div>
          <div><strong>{{ plan.title }}</strong><p>{{ plan.goal || plan.description || '等待 Agent 完善目标' }}</p></div>
        </button>
      </div>
      <div v-else class="empty-state compact">让 Agent 根据你的目标创建第一份完整学习计划。</div>
    </article>

    <AgentComposer />
  </section>
</template>
