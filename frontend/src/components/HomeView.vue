<script setup>
import {
  BoltIcon,
  CheckCircleIcon,
  CircleStackIcon,
  CommandLineIcon,
  MapIcon,
  SparklesIcon,
  XCircleIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentComposer from './AgentComposer.vue';

const store = useWorkspaceStore();
const processExpanded = ref(false);
const running = computed(() => ['queued', 'running'].includes(store.currentRun?.status));
const days = computed(() => store.dashboard.activity.map((day, index) => ({
  index,
  date: day.date,
  value: Math.min(day.count, 4),
})));
const activityEvents = computed(() => store.runEvents.filter((event) => ![
  'run.started',
  'tool.started',
  'assistant.message',
  'run.completed',
].includes(event.type)));
const visibleActivityEvents = computed(() => (
  processExpanded.value ? activityEvents.value : activityEvents.value.slice(0, 6)
));
const finalEvent = computed(() => [...store.runEvents].reverse().find(
  (event) => event.type === 'assistant.message' || event.type === 'run.completed' || event.type === 'run.failed',
));
const suggestions = [
  '根据我的目标创建一份完整学习计划',
  '检查我现在的计划，告诉我今天最该做什么',
  '根据最近表现主动抽查我',
];

watch(() => store.currentRun?.id, () => {
  processExpanded.value = false;
});

function iconFor(type) {
  if (type === 'context.built') return CircleStackIcon;
  if (type.startsWith('tool.')) return CommandLineIcon;
  if (type === 'run.failed') return XCircleIcon;
  return CheckCircleIcon;
}

function eventTitle(event) {
  if (event.type === 'context.built') return '读取学习上下文';
  if (event.type === 'tool.started') return `正在调用 ${event.payload?.name || '工具'}`;
  if (event.type === 'tool.completed') return `${event.payload?.name || '工具'} 已完成`;
  if (event.type === 'assistant.status') return event.summary;
  if (event.type === 'run.failed') return '运行失败';
  return event.summary || event.type;
}
</script>

<template>
  <section class="conversation-page">
    <div class="conversation-scroll">
      <div v-if="store.error" class="error-banner">{{ store.error }}</div>

      <div v-if="!store.currentRun" class="welcome-state">
        <div class="welcome-mark"><SparklesIcon /></div>
        <h1>今天想学什么？</h1>
        <p>告诉我你的目标。我会制定计划、持续跟进，并在需要时主动提醒或考核。</p>
        <div class="suggestion-list">
          <button v-for="suggestion in suggestions" :key="suggestion" @click="store.startRun(suggestion)">
            {{ suggestion }}
          </button>
        </div>

        <div class="quiet-overview">
          <div class="overview-copy">
            <span><BoltIcon /> 主动教练已就绪</span>
            <p>后台心跳会自行判断何时介入；没有必要时，它会保持安静。</p>
          </div>
          <div class="overview-stats">
            <button @click="store.openView('plans')"><strong>{{ store.activePlans.length }}</strong><span>进行中计划</span></button>
            <button @click="store.openView('inbox')"><strong>{{ store.unreadCount }}</strong><span>待处理消息</span></button>
            <div class="mini-heatmap" title="最近 12 周真实学习活动">
              <i v-for="day in days" :key="day.index" :class="`heat-${day.value}`"></i>
            </div>
          </div>
        </div>
      </div>

      <div v-else class="thread">
        <div class="user-turn">
          <div class="message-bubble">{{ store.currentRun.objective }}</div>
        </div>

        <div class="agent-turn">
          <div class="agent-avatar"><SparklesIcon /></div>
          <div class="agent-content">
            <div class="agent-name">Learning Agent <span>Hy3</span></div>

            <div v-if="activityEvents.length || running" class="inline-process">
              <div
                v-for="event in visibleActivityEvents"
                :key="event.sequence"
                :class="['process-row', { failed: event.type === 'run.failed' }]"
              >
                <component :is="iconFor(event.type)" />
                <span>{{ eventTitle(event) }}</span>
                <small v-if="event.type === 'tool.completed'">
                  {{ event.payload?.result?.ok === false ? '失败' : '完成' }}
                </small>
              </div>
              <div v-if="running" class="process-row active-process">
                <span class="thinking-dots"><i></i><i></i><i></i></span>
                <span>正在观察、规划并调用工具</span>
              </div>
              <div class="process-links">
                <button v-if="activityEvents.length > 6" @click="processExpanded = !processExpanded">
                  {{ processExpanded ? '收起步骤' : `展开其余 ${activityEvents.length - 6} 个动作` }}
                </button>
                <button @click="store.traceOpen = true">查看完整运行轨迹</button>
              </div>
            </div>

            <div v-if="finalEvent" :class="['assistant-answer', { failed: finalEvent.type === 'run.failed' }]">
              <p>{{ finalEvent.summary }}</p>
              <small v-if="finalEvent.type === 'run.failed'">{{ finalEvent.payload?.error }}</small>
            </div>
          </div>
        </div>

        <div v-if="store.currentPlan" class="context-chip"><MapIcon /> 当前计划：{{ store.currentPlan.title }}</div>
      </div>
    </div>

    <AgentComposer />
  </section>
</template>
