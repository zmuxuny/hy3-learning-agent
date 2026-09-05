<script setup>
import {
  ArchiveBoxArrowDownIcon,
  ArrowUturnLeftIcon,
  BellIcon,
  BoltIcon,
  ChatBubbleLeftRightIcon,
  CircleStackIcon,
  CogIcon,
  MapIcon,
  PencilSquareIcon,
  PencilIcon,
  MagnifyingGlassIcon,
} from '@heroicons/vue/24/outline';
import { computed, nextTick, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useInboxStore } from '../stores/inbox.js';
import { usePlanStore } from '../stores/plan.js';
import { useSessionStore } from '../stores/session.js';
import { useSettingsStore } from '../stores/settings.js';
import { useShellStore } from '../stores/shell.js';
import { isRunBlocking, isRunStreamable } from '../runState.js';

const route = useRoute();
const router = useRouter();
const inbox = useInboxStore();
const planStore = usePlanStore();
const sessionStore = useSessionStore();
const settings = useSettingsStore();
const shell = useShellStore();
const editingSessionId = ref(null);
const sessionTitle = ref('');
const sessionTitleInput = ref(null);
const showArchivedSessions = ref(false);
const searchOpen = ref(false);
const searchQuery = ref('');
const searchInput = ref(null);
const displayedSessions = computed(() => (
  (showArchivedSessions.value ? sessionStore.archivedSessions : sessionStore.sessions).filter((session) => (
    !searchQuery.value.trim()
    || `${session.title} ${sessionMeta(session)}`.toLowerCase().includes(searchQuery.value.trim().toLowerCase())
  ))
));
const unreadBySession = computed(() => {
  const counts = {};
  for (const item of inbox.notifications) {
    if (item.session_id && !item.read_at) {
      counts[item.session_id] = (counts[item.session_id] || 0) + 1;
    }
  }
  return counts;
});
const navigation = [
  { id: 'plans', label: '学习计划', icon: MapIcon },
  { id: 'inbox', label: '收件箱', icon: BellIcon },
  { id: 'memory', label: '学习记忆', icon: CircleStackIcon },
  { id: 'settings', label: '设置', icon: CogIcon },
];

const heartbeatLabel = computed(() => {
  const status = settings.schedulerStatus;
  if (!status?.enabled) return '后台检查已关闭';
  if (status.paused) return '后台主动检查已暂停';
  if (status.active) return '正在主动检查学习状态';
  if (!status.next_cycle_at) return `每 ${Math.round((status.interval_seconds || 300) / 60)} 分钟检查`;
  const next = new Date(status.next_cycle_at);
  return `下次检查 ${next.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
});

function sessionMeta(session) {
  const plan = planStore.allPlans.find((item) => item.id === session.plan_id);
  if (plan) return plan.title;
  return session.message_count > 1 ? `${session.message_count} 条消息` : '全局对话';
}

function sessionNeedsInput(session) {
  return (unreadBySession.value[session.id] || 0) > 0;
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
  if (title && title !== session.title) await sessionStore.renameSession(session.id, title);
  editingSessionId.value = null;
}

function cancelRename() {
  editingSessionId.value = null;
}

function setSessionTitleInput(element) {
  sessionTitleInput.value = element;
}

async function toggleSearch() {
  searchOpen.value = !searchOpen.value;
  if (!searchOpen.value) searchQuery.value = '';
  await nextTick();
  searchInput.value?.focus();
}
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-heading">
      <button class="brand" title="返回学习对话" @click="router.push({ name: 'home' })">
        <strong>Learning Agent</strong>
      </button>
      <button :class="['sidebar-utility', { active: searchOpen }]" title="搜索对话" @click="toggleSearch"><MagnifyingGlassIcon /></button>
      <button class="sidebar-utility" title="打开学习收件箱" @click="router.push({ name: 'inbox' })"><BellIcon /></button>
    </div>
    <label v-if="searchOpen" class="sidebar-search">
      <MagnifyingGlassIcon />
      <input ref="searchInput" v-model="searchQuery" placeholder="搜索对话" @keydown.esc="toggleSearch" />
    </label>

    <button class="new-run" title="新对话" aria-label="新对话" @click="shell.startNewConversation">
      <PencilSquareIcon /><span>新对话</span>
    </button>

    <nav class="nav-list">
      <button
        :class="['nav-item', 'mobile-conversation-nav', { active: ['home', 'session'].includes(route.name) }]"
        @click="router.push({ name: 'home' })"
      >
        <ChatBubbleLeftRightIcon />
        <span>对话</span>
      </button>
      <button
        v-for="item in navigation"
        :key="item.id"
        :class="['nav-item', { active: route.name === item.id || (item.id === 'plans' && ['plan', 'archives'].includes(route.name)) || (item.id === 'inbox' && route.name === 'inbox-intervention') }]"
        @click="router.push({ name: item.id })"
      >
        <component :is="item.icon" />
        <span>{{ item.label }}</span>
        <em v-if="item.id === 'inbox' && inbox.unreadCount">{{ inbox.unreadCount }}</em>
      </button>
    </nav>

    <section class="sidebar-group" v-if="planStore.activePlans.length">
      <div class="section-title">置顶计划</div>
      <div
        v-for="plan in planStore.activePlans.slice(0, 3)"
        :key="plan.id"
        class="side-plan-row"
      >
        <button class="side-row" @click="shell.selectPlan(plan.id)">
          <MapIcon />
          <span><strong>{{ plan.title }}</strong><small>{{ Math.round(plan.progress * 100) }}% 完成</small></span>
        </button>
        <button class="side-plan-archive" title="归档计划" @click="shell.setPlanArchived(plan.id, true)">
          <ArchiveBoxArrowDownIcon />
        </button>
      </div>
    </section>

    <section class="sidebar-group recent-group">
      <div class="section-title session-section-title">
        <span>{{ showArchivedSessions ? '已归档对话' : '对话' }}</span>
        <button @click="showArchivedSessions = !showArchivedSessions">
          {{ showArchivedSessions ? '返回' : `归档 ${sessionStore.archivedSessions.length || ''}` }}
        </button>
      </div>
      <div
        v-for="session in displayedSessions"
        :key="session.id"
        :class="['session-row', { active: sessionStore.activeSessionId === session.id }]"
        role="button"
        tabindex="0"
        @click="shell.selectSession(session)"
        @keydown.enter="shell.selectSession(session)"
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
        <i
          v-if="sessionNeedsInput(session)"
          class="session-status-dot needs-input"
          title="有待处理消息"
        ></i>
        <i
          v-else-if="isRunStreamable(session.last_run_status)"
          class="session-status-dot running"
          title="运行中"
        ></i>
        <i
          v-else-if="['waiting_approval', 'needs_reconciliation'].includes(session.last_run_status)"
          class="session-status-dot waiting"
          :title="session.last_run_status === 'waiting_approval' ? '等待你的确认' : '需要人工处理'"
        ></i>
        <div v-if="!isRunBlocking(session.last_run_status)" class="session-actions" @click.stop>
          <button v-if="!session.archived_at" title="重命名对话" @click="beginRename(session)"><PencilIcon /></button>
          <button
            :title="session.archived_at ? '恢复对话' : '归档对话'"
            @click="shell.setSessionArchived(session.id, !session.archived_at)"
          >
            <component :is="session.archived_at ? ArrowUturnLeftIcon : ArchiveBoxArrowDownIcon" />
          </button>
        </div>
      </div>
      <p v-if="!displayedSessions.length" class="empty-sidebar">
        {{ showArchivedSessions ? '还没有归档对话' : '对话会出现在这里' }}
      </p>
    </section>

    <div class="sidebar-footer">
      <button class="agent-status" @click="shell.triggerHeartbeat">
        <span class="agent-status-icon"><BoltIcon /></span>
        <span><strong>检查学习进度</strong><small>{{ heartbeatLabel }}</small></span>
        <i></i>
      </button>
      <button class="profile-card" v-if="settings.profile" @click="router.push({ name: 'settings' })">
        <div class="avatar">{{ settings.profile.level }}</div>
        <span><strong>个人设置</strong><small>Lv.{{ settings.profile.level }} · {{ settings.profile.xp }} XP</small></span>
        <CogIcon />
      </button>
    </div>
  </aside>
</template>
