<script setup>
import {
  AcademicCapIcon,
  ArchiveBoxArrowDownIcon,
  ArrowUturnLeftIcon,
  BellIcon,
  BoltIcon,
  ChatBubbleLeftRightIcon,
  ChevronDownIcon,
  CircleStackIcon,
  ClockIcon,
  CogIcon,
  MapIcon,
  PencilSquareIcon,
  PencilIcon,
} from '@heroicons/vue/24/outline';
import { computed, nextTick, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const editingSessionId = ref(null);
const sessionTitle = ref('');
const sessionTitleInput = ref(null);
const showArchivedSessions = ref(false);
const displayedSessions = computed(() => (
  showArchivedSessions.value ? store.archivedSessions : store.sessions
));
const unreadBySession = computed(() => {
  const counts = {};
  for (const item of store.notifications) {
    if (item.session_id && !item.read_at) {
      counts[item.session_id] = (counts[item.session_id] || 0) + 1;
    }
  }
  return counts;
});
const navigation = [
  { id: 'plans', label: '学习计划', icon: MapIcon },
  { id: 'inbox', label: '收件箱', icon: BellIcon },
  { id: 'memory', label: 'AI 记忆', icon: CircleStackIcon },
  { id: 'settings', label: '设置', icon: CogIcon },
];

const heartbeatLabel = computed(() => {
  const status = store.schedulerStatus;
  if (!status?.enabled) return '后台检查已关闭';
  if (status.active) return '正在主动检查学习状态';
  if (!status.next_cycle_at) return `每 ${Math.round((status.interval_seconds || 300) / 60)} 分钟检查`;
  const next = new Date(status.next_cycle_at);
  return `下次检查 ${next.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
});

function sessionMeta(session) {
  const plan = [...store.plans, ...store.archivedPlans].find((item) => item.id === session.plan_id);
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
      <div
        v-for="plan in store.activePlans.slice(0, 3)"
        :key="plan.id"
        class="side-plan-row"
      >
        <button class="side-row" @click="store.selectPlan(plan.id)">
          <MapIcon />
          <span><strong>{{ plan.title }}</strong><small>{{ Math.round(plan.progress * 100) }}% 完成</small></span>
        </button>
        <button class="side-plan-archive" title="归档计划" @click="store.setPlanArchived(plan.id, true)">
          <ArchiveBoxArrowDownIcon />
        </button>
      </div>
    </section>

    <section class="sidebar-group recent-group">
      <div class="section-title session-section-title">
        <span>{{ showArchivedSessions ? '已归档对话' : '对话' }}</span>
        <button @click="showArchivedSessions = !showArchivedSessions">
          {{ showArchivedSessions ? '返回' : `归档 ${store.archivedSessions.length || ''}` }}
        </button>
      </div>
      <div
        v-for="session in displayedSessions"
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
        <i
          v-if="sessionNeedsInput(session)"
          class="session-status-dot needs-input"
          title="有待处理消息"
        ></i>
        <i
          v-else-if="['queued', 'running'].includes(session.last_run_status)"
          class="session-status-dot running"
          title="运行中"
        ></i>
        <i
          v-else-if="session.last_run_status === 'waiting_approval'"
          class="session-status-dot waiting"
          title="等待你的确认"
        ></i>
        <div v-if="!['queued', 'running', 'waiting_approval'].includes(session.last_run_status)" class="session-actions" @click.stop>
          <button v-if="!session.archived_at" title="重命名对话" @click="beginRename(session)"><PencilIcon /></button>
          <button
            :title="session.archived_at ? '恢复对话' : '归档对话'"
            @click="store.setSessionArchived(session.id, !session.archived_at)"
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
      <button class="agent-status" @click="store.triggerHeartbeat">
        <span class="agent-status-icon"><BoltIcon /></span>
        <span><strong>学习 Agent 在线</strong><small>{{ heartbeatLabel }} · 点击立即检查</small></span>
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
