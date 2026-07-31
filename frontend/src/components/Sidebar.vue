<script setup>
import {
  AcademicCapIcon,
  BellIcon,
  BoltIcon,
  ChevronDownIcon,
  CircleStackIcon,
  ClockIcon,
  MapIcon,
  PencilSquareIcon,
} from '@heroicons/vue/24/outline';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const navigation = [
  { id: 'plans', label: '学习计划', icon: MapIcon },
  { id: 'inbox', label: '收件箱', icon: BellIcon },
  { id: 'memory', label: 'AI 记忆', icon: CircleStackIcon },
];
</script>

<template>
  <aside class="sidebar">
    <button class="brand" @click="store.openView('home')">
      <span class="brand-mark"><AcademicCapIcon /></span>
      <strong>Learning Agent</strong>
      <ChevronDownIcon />
    </button>

    <button class="new-run" @click="store.startNewConversation">
      <PencilSquareIcon /> 新对话
    </button>

    <nav class="nav-list">
      <button
        v-for="item in navigation"
        :key="item.id"
        :class="['nav-item', { active: store.activeView === item.id }]"
        @click="store.openView(item.id)"
      >
        <component :is="item.icon" />
        <span>{{ item.label }}</span>
        <em v-if="item.id === 'inbox' && store.unreadCount">{{ store.unreadCount }}</em>
      </button>
    </nav>

    <section class="sidebar-group" v-if="store.activePlans.length">
      <div class="section-title">置顶计划</div>
      <button
        v-for="plan in store.activePlans.slice(0, 3)"
        :key="plan.id"
        class="side-row"
        @click="store.selectPlan(plan.id)"
      >
        <MapIcon />
        <span><strong>{{ plan.title }}</strong><small>{{ Math.round(plan.progress * 100) }}% 完成</small></span>
      </button>
    </section>

    <section class="sidebar-group recent-group">
      <div class="section-title">最近</div>
      <button
        v-for="run in store.runs.slice(0, 8)"
        :key="run.id"
        :class="['recent-row', { active: store.currentRun?.id === run.id }]"
        @click="store.inspectRun(run)"
      >
        <span>{{ run.objective }}</span>
        <i v-if="['queued', 'running'].includes(run.status)"></i>
      </button>
      <p v-if="!store.runs.length" class="empty-sidebar">对话会出现在这里</p>
    </section>

    <div class="sidebar-footer">
      <button class="agent-status" @click="store.triggerHeartbeat">
        <span class="agent-status-icon"><BoltIcon /></span>
        <span><strong>学习 Agent 在线</strong><small>点击立即检查计划</small></span>
        <i></i>
      </button>
      <div class="profile-card" v-if="store.profile">
        <div class="avatar">{{ store.profile.level }}</div>
        <span><strong>本地学习者</strong><small>Lv.{{ store.profile.level }} · {{ store.profile.xp }} XP</small></span>
        <ClockIcon />
      </div>
    </div>
  </aside>
</template>
