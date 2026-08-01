<script setup>
import {
  AcademicCapIcon,
  BellIcon,
  BoltIcon,
  ChatBubbleLeftRightIcon,
  ChevronDownIcon,
  CircleStackIcon,
  ClockIcon,
  MapIcon,
  PencilSquareIcon,
  PencilIcon,
} from '@heroicons/vue/24/outline';
import { nextTick, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const editingSessionId = ref(null);
const sessionTitle = ref('');
const sessionTitleInput = ref(null);
const navigation = [
  { id: 'plans', label: '学习计划', icon: MapIcon },
  { id: 'inbox', label: '收件箱', icon: BellIcon },
  { id: 'memory', label: 'AI 记忆', icon: CircleStackIcon },
];

function sessionMeta(session) {
  const plan = store.plans.find((item) => item.id === session.plan_id);
  if (plan) return plan.title;
  return session.message_count > 1 ? `${session.message_count} 条消息` : '全局对话';
}

async function beginRename(session) {
  editingSessionId.value = session.id;
  sessionTitle.value = session.title;
  await nextTick();
  sessionTitleInput.value?.focus();
  sessionTitleInput.value?.select();
}

async function saveRename(session) {
  const title = sessionTitle.value.trim();
  if (title && title !== session.title) await store.renameSession(session.id, title);
  editingSessionId.value = null;
}

function cancelRename() {
  editingSessionId.value = null;
}

function setSessionTitleInput(element) {
  sessionTitleInput.value = element;
}
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
      <div class="section-title">对话</div>
      <div
        v-for="session in store.sessions.slice(0, 12)"
        :key="session.id"
        :class="['session-row', { active: store.activeSessionId === session.id }]"
        role="button"
        tabindex="0"
        @click="store.selectSession(session)"
        @keydown.enter="store.selectSession(session)"
      >
        <ChatBubbleLeftRightIcon class="session-icon" />
        <form
          v-if="editingSessionId === session.id"
          class="session-rename"
          @click.stop
          @submit.prevent="saveRename(session)"
        >
          <input
            :ref="setSessionTitleInput"
            v-model="sessionTitle"
            maxlength="80"
            aria-label="修改对话名称"
            @keydown.esc.prevent="cancelRename"
            @blur="saveRename(session)"
          />
        </form>
        <span v-else class="session-copy">
          <strong>{{ session.title }}</strong>
          <small>{{ sessionMeta(session) }}</small>
        </span>
        <i v-if="['queued', 'running'].includes(session.last_run_status)" class="session-running"></i>
        <button
          v-else
          class="session-rename-button"
          title="重命名对话"
          @click.stop="beginRename(session)"
        >
          <PencilIcon />
        </button>
      </div>
      <p v-if="!store.sessions.length" class="empty-sidebar">对话会出现在这里</p>
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
