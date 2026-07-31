<script setup>
import {
  AcademicCapIcon,
  BellIcon,
  BoltIcon,
  ChartBarSquareIcon,
  CircleStackIcon,
  MapIcon,
  PlusIcon,
} from '@heroicons/vue/24/outline';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const navigation = [
  { id: 'home', label: '学习总览', icon: ChartBarSquareIcon },
  { id: 'plans', label: '学习计划', icon: MapIcon },
  { id: 'memory', label: 'AI 眼中的我', icon: CircleStackIcon },
  { id: 'inbox', label: '通知收件箱', icon: BellIcon },
];
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark"><AcademicCapIcon /></div>
      <div>
        <strong>Learning Agent</strong>
        <span>powered by Hy3</span>
      </div>
    </div>

    <button class="new-run" @click="store.startNewConversation">
      <PlusIcon /> 新建 Agent 任务
    </button>

    <nav class="nav-list">
      <button
        v-for="item in navigation"
        :key="item.id"
        :class="['nav-item', { active: store.activeView === item.id }]"
        @click="store.activeView = item.id"
      >
        <component :is="item.icon" />
        <span>{{ item.label }}</span>
        <em v-if="item.id === 'inbox' && store.unreadCount">{{ store.unreadCount }}</em>
      </button>
    </nav>

    <div class="sidebar-section">
      <div class="section-title">
        <span>进行中的计划</span>
        <small>{{ store.activePlans.length }}</small>
      </div>
      <button
        v-for="plan in store.activePlans.slice(0, 5)"
        :key="plan.id"
        class="plan-link"
        @click="store.selectPlan(plan.id)"
      >
        <span class="plan-dot"></span>
        <span class="plan-link-copy">
          <strong>{{ plan.title }}</strong>
          <small>{{ Math.round(plan.progress * 100) }}% · v{{ plan.version }}</small>
        </span>
      </button>
      <p v-if="!store.activePlans.length" class="empty-sidebar">还没有学习计划</p>
    </div>

    <div class="agent-state">
      <div class="state-icon"><BoltIcon /></div>
      <div>
        <strong>主动 Agent 已就绪</strong>
        <span>下次心跳由后端调度</span>
      </div>
      <i></i>
    </div>

    <div class="profile-card" v-if="store.profile">
      <div class="avatar">{{ store.profile.level }}</div>
      <div>
        <strong>监督型学习者</strong>
        <span>Lv.{{ store.profile.level }} · {{ store.profile.xp }} XP</span>
      </div>
    </div>
  </aside>
</template>
