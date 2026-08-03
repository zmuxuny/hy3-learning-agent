<script setup>
import {
  CheckCircleIcon,
  ArrowRightIcon,
  ChevronDownIcon,
  CircleStackIcon,
  ClipboardIcon,
  CommandLineIcon,
  MapIcon,
  SparklesIcon,
  UserGroupIcon,
  XCircleIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentMessage from './AgentMessage.vue';

const props = defineProps({
  answer: { type: String, default: '' },
  userMessage: { type: Object, default: null },
});
const store = useWorkspaceStore();
const processExpanded = ref(false);
const answersExpanded = ref(false);
const childExpanded = ref(new Set());
const childDetails = ref({});
const running = computed(() => ['queued', 'running', 'waiting_approval'].includes(store.currentRun?.status));
const approvalPending = computed(() => (
  store.currentRun?.status === 'waiting_approval' && Boolean(store.currentRun?.pending_approval)
));
const approvalEvent = computed(() => [...store.runEvents].reverse().find(
  (event) => event.type === 'approval.required' && event.payload?.blocking,
));
const activityEvents = computed(() => {
  const rows = [];
  for (const event of store.runEvents) {
    if (['run.started', 'assistant.message', 'run.completed'].includes(event.type)) continue;
    if (event.type === 'tool.completed') {
      const startedIndex = rows.findIndex((item) => (
        item.type === 'tool.started' && item.payload?.tool_call_id === event.payload?.tool_call_id
      ));
      if (startedIndex >= 0) {
        rows.splice(startedIndex, 1, event);
        continue;
      }
    }
    rows.push(event);
  }
  return rows;
});
const latestActivity = computed(() => activityEvents.value[activityEvents.value.length - 1] || null);
const finalEvent = computed(() => [...store.runEvents].reverse().find(
  (event) => ['assistant.message', 'run.completed', 'run.failed', 'run.cancelled'].includes(event.type),
));
const answerText = computed(() => props.answer || finalEvent.value?.summary || '');
const planningAnswers = computed(() => (
  props.userMessage?.message_metadata?.ui_kind === 'planning_answers'
    ? (props.userMessage.message_metadata.answers || [])
    : null
));
const contextEvent = computed(() => [...store.runEvents].reverse().find(
  (event) => event.type === 'context.built',
));
const usedMemories = computed(() => {
  const ids = new Set((contextEvent.value?.payload?.memory_ids || []).map(String));
  return store.memories.filter((memory) => ids.has(String(memory.id)));
});
const subagents = computed(() => {
  const started = store.runEvents.filter((event) => event.type === 'subagent.started');
  return started.map((event) => {
    const childId = event.payload?.child_run_id;
    const completed = [...store.runEvents].reverse().find(
      (item) => item.type === 'subagent.completed' && item.payload?.child_run_id === childId,
    );
    return {
      child_run_id: childId,
      role: event.payload?.role || '子 Agent',
      objective: event.payload?.objective || '',
      status: completed?.payload?.status || 'running',
    };
  });
});
const durationLabel = computed(() => {
  const start = store.currentRun?.started_at || store.currentRun?.created_at;
  if (!start) return '';
  const end = store.currentRun?.completed_at
    || latestActivity.value?.created_at
    || (running.value ? new Date().toISOString() : start);
  const seconds = Math.max(0, Math.round((new Date(end) - new Date(start)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
});
const activitySummary = computed(() => {
  if (approvalPending.value) return '等待你批准一个操作';
  if (running.value) return latestActivity.value ? eventTitle(latestActivity.value) : '正在准备学习上下文';
  if (store.currentRun?.status === 'failed') return '本次运行已失败';
  if (store.currentRun?.status === 'cancelled') return '本次运行已停止';
  return `已处理 ${activityEvents.value.length} 个动作`;
});

watch(() => store.currentRun?.id, () => {
  processExpanded.value = false;
});

function iconFor(type) {
  if (type === 'context.built') return CircleStackIcon;
  if (type === 'run.retrying') return CircleStackIcon;
  if (type.startsWith('tool.')) return CommandLineIcon;
  if (type.startsWith('subagent.')) return UserGroupIcon;
  if (type === 'run.failed' || type === 'run.cancelled') return XCircleIcon;
  return CheckCircleIcon;
}

function eventTitle(event) {
  if (event.type === 'context.built') return '读取学习上下文';
  if (event.type === 'run.retrying') return event.summary;
  if (event.type === 'tool.started') return `正在调用 ${event.payload?.name || '工具'}`;
  if (event.type === 'tool.completed') return event.payload?.name || '工具';
  if (event.type === 'subagent.started') return `${event.payload?.role || '规划'}子 Agent 已接受分工`;
  if (event.type === 'subagent.completed') return `${event.payload?.role || '规划'}子 Agent 已返回结论`;
  if (event.type === 'assistant.status') return event.summary;
  if (event.type === 'run.failed') return '运行失败';
  if (event.type === 'run.cancelled') return '运行已停止';
  return event.summary || event.type;
}

async function continueInCreatedPlan() {
  if (store.createdPlanFromCurrentRun) {
    await store.continueInPlan(store.createdPlanFromCurrentRun.id);
  }
}

async function toggleChild(childId) {
  const next = new Set(childExpanded.value);
  next.has(childId) ? next.delete(childId) : next.add(childId);
  childExpanded.value = next;
  if (next.has(childId) && !childDetails.value[childId]) {
    try {
      const events = await store.fetchChildRunEvents(childId);
      childDetails.value = { ...childDetails.value, [childId]: events };
    } catch {
      childDetails.value = { ...childDetails.value, [childId]: [] };
    }
  }
}

function childToolNames(childId) {
  return [...new Set((childDetails.value[childId] || [])
    .filter((event) => event.event_type === 'tool.started')
    .map((event) => event.payload?.name)
    .filter(Boolean))];
}

function childReport(childId) {
  const events = childDetails.value[childId] || [];
  const finalEvent = [...events].reverse().find(
    (event) => ['run.completed', 'run.failed', 'run.cancelled'].includes(event.event_type),
  );
  return finalEvent?.summary || '';
}

async function copyAnswer() {
  const text = answerText.value.trim();
  if (text) await navigator.clipboard?.writeText(text);
}
</script>

<template>
  <div class="agent-turn run-turn">
    <div class="agent-avatar"><SparklesIcon /></div>
    <div class="agent-content">
      <div class="agent-name">Learning Agent <span>Hy3</span><button class="answer-copy" title="复制回答" @click="copyAnswer"><ClipboardIcon /></button></div>

      <div v-if="store.currentRun" :class="['run-activity', { expanded: processExpanded, running }]">
        <button class="run-activity-summary" @click="processExpanded = !processExpanded">
          <span v-if="running" class="thinking-dots"><i></i><i></i><i></i></span>
          <CheckCircleIcon v-else-if="store.currentRun.status === 'completed'" />
          <XCircleIcon v-else-if="['failed', 'cancelled'].includes(store.currentRun.status)" />
          <CircleStackIcon v-else />
          <span><strong>{{ activitySummary }}</strong><small>{{ durationLabel }}<template v-if="activityEvents.length"> · {{ activityEvents.length }} 个记录</template></small></span>
          <ChevronDownIcon class="activity-chevron" />
        </button>
        <div v-if="processExpanded" class="run-activity-body">
          <div
            v-for="event in activityEvents"
            :key="event.sequence"
            :class="['process-row', { failed: event.type === 'run.failed' || event.type === 'run.cancelled' || event.payload?.result?.ok === false }]"
          >
            <component :is="iconFor(event.type)" />
            <span>{{ eventTitle(event) }}</span>
            <small v-if="event.type === 'tool.completed'">{{ event.payload?.result?.ok === false ? '失败' : '完成' }}</small>
          </div>
          <button class="full-trace-link" @click="store.traceOpen = true">查看完整运行轨迹</button>

          <div v-if="usedMemories.length" class="activity-section">
            <strong>本次使用记忆</strong>
            <div v-for="memory in usedMemories" :key="memory.id" class="memory-chip" :title="memory.content">
              <span>{{ memory.layer }}/{{ memory.scope }}</span>{{ memory.content }}
            </div>
          </div>

          <div v-if="subagents.length" class="activity-section">
            <strong>子 Agent</strong>
            <div v-for="agent in subagents" :key="agent.child_run_id" class="subagent-row">
              <button @click="toggleChild(agent.child_run_id)">
                <UserGroupIcon />
                <span><strong>{{ agent.role }}</strong><small>{{ agent.status }}</small></span>
                <ChevronDownIcon />
              </button>
              <div v-if="childExpanded.has(agent.child_run_id)" class="subagent-detail">
                <p>{{ agent.objective }}</p>
                <small v-if="childToolNames(agent.child_run_id).length">工具：{{ childToolNames(agent.child_run_id).join('、') }}</small>
                <p v-if="childReport(agent.child_run_id)" class="subagent-report">{{ childReport(agent.child_run_id) }}</p>
              </div>
            </div>
          </div>
        </div>
      </div>

      <section v-if="planningAnswers" class="planning-answers-card">
        <button class="planning-answers-summary" @click="answersExpanded = !answersExpanded">
          <CircleStackIcon />
          <span><strong>计划澄清已提交 · {{ planningAnswers.length }} 个回答</strong><small>{{ answersExpanded ? '收起' : '点击展开查看回答' }}</small></span>
          <ChevronDownIcon class="activity-chevron" />
        </button>
        <div v-if="answersExpanded" class="planning-answers-body">
          <div v-for="answer in planningAnswers" :key="answer.question_id" class="planning-answer-row">
            <small>{{ answer.question_id }}</small>
            <p>{{ answer.answer }}</p>
          </div>
        </div>
      </section>

      <section v-if="approvalPending" class="approval-card">
        <div class="approval-copy">
          <small>需要你的确认</small>
          <strong>{{ approvalEvent?.payload?.tool_name || 'Agent 操作' }}</strong>
          <p>{{ approvalEvent?.payload?.reason || store.currentRun.pending_approval?.reason || '该操作需要你批准后才会执行。' }}</p>
        </div>
        <div class="approval-actions">
          <button class="secondary-button" @click="store.decideRunApproval(store.currentRun.id, false)">拒绝</button>
          <button class="primary-button" @click="store.decideRunApproval(store.currentRun.id, true)">批准</button>
        </div>
      </section>

      <div v-if="answerText" :class="['assistant-answer', { failed: finalEvent?.type === 'run.failed' }]">
        <AgentMessage :content="answerText" />
        <small v-if="finalEvent?.type === 'run.failed'">错误编号：{{ finalEvent.payload?.code || 'run_failed' }}</small>
      </div>

      <section v-if="store.createdPlanFromCurrentRun" class="context-transition">
        <div class="context-transition-icon"><MapIcon /></div>
        <div>
          <small>计划已建立 · 当前仍是全局对话</small>
          <strong>{{ store.createdPlanFromCurrentRun.title }}</strong>
          <p>你可以留在这里协调多个计划，或建立一个带交接摘要的计划对话。</p>
        </div>
        <div class="context-transition-actions">
          <button @click="store.selectPlan(store.createdPlanFromCurrentRun.id)">打开计划</button>
          <button class="primary" @click="continueInCreatedPlan">在计划中继续 <ArrowRightIcon /></button>
        </div>
      </section>
    </div>
  </div>
</template>
