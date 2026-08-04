<script setup>
import {
  CheckCircleIcon,
  ChevronDownIcon,
  CircleStackIcon,
  ClipboardIcon,
  CommandLineIcon,
  SparklesIcon,
  UserGroupIcon,
  XCircleIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentMessage from './AgentMessage.vue';
import PlanCard from './PlanCard.vue';
import PlanningProposalPanel from './PlanningProposalPanel.vue';
import PlanningQuestionsPanel from './PlanningQuestionsPanel.vue';

const props = defineProps({
  answer: { type: String, default: '' },
  userMessage: { type: Object, default: null },
  cards: { type: Array, default: () => [] },
});
const store = useWorkspaceStore();
const processExpanded = ref(false);
const childExpanded = ref(new Set());
const childDetails = ref({});
const approvalAnswer = ref('');
const displayAnswer = ref('');
let answerFrame = 0;
const running = computed(() => ['queued', 'running', 'waiting_approval'].includes(store.currentRun?.status));
const streaming = computed(() => (
  store.streamingRunId === store.currentRun?.id && Boolean(store.streamingText)
));
const thinking = computed(() => (
  store.streamingRunId === store.currentRun?.id
  && Boolean(store.streamingReasoning)
  && !Boolean(store.streamingText)
));
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
const answerText = computed(() => (
  store.streamingRunId === store.currentRun?.id && store.streamingText
    ? store.streamingText
    : props.answer || finalEvent.value?.summary || ''
));
watch(answerText, (text) => {
  cancelAnimationFrame(answerFrame);
  answerFrame = requestAnimationFrame(() => {
    displayAnswer.value = text;
  });
}, { immediate: true });
const liveIntakeMatches = computed(() => (
  props.cards.length === 0
  && store.planningState.intake?.source_run_id === store.currentRun?.id
  && (store.planningState.intake?.open_questions || []).length > 0
));
const liveProposalMatches = computed(() => (
  props.cards.length === 0
  && store.planningState.proposal?.source_run_id === store.currentRun?.id
  && store.planningState.proposal?.status !== 'accepted'
));
const snapshotCards = computed(() => props.cards.map((card) => {
  if (card.kind === 'planning_questions') {
    const live = store.planningState.intake;
    const current = Boolean(
      live && live.source_run_id === card.source_run_id && (live.open_questions || []).length > 0,
    );
    return { ...card, current };
  }
  if (card.kind === 'plan_proposal') {
    const live = store.planningState.proposal;
    const current = Boolean(live && live.id === card.proposal?.id && live.status === 'pending');
    return { ...card, current };
  }
  return { ...card, current: false };
}));
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

      <template v-for="card in snapshotCards" :key="`${card.kind}-${card.created_at}`">
        <PlanningQuestionsPanel
          v-if="card.kind === 'planning_questions'"
          :intake="card.current ? null : card.intake"
          :readonly="!card.current"
        />
        <PlanningProposalPanel
          v-else-if="card.kind === 'plan_proposal'"
          :proposal="card.current ? null : card.proposal"
          :readonly="!card.current"
        />
      </template>
      <template v-if="props.cards.length === 0">
        <PlanningQuestionsPanel v-if="liveIntakeMatches" />
        <PlanningProposalPanel v-if="liveProposalMatches" />
      </template>

      <section v-if="approvalPending" class="approval-card">
        <div class="approval-copy">
          <small>需要你的确认</small>
          <strong>{{ approvalEvent?.payload?.tool_name || 'Agent 操作' }}</strong>
          <p>{{ approvalEvent?.payload?.reason || store.currentRun.pending_approval?.reason || '该操作需要你批准后才会执行。' }}</p>
        </div>
        <textarea
          v-model="approvalAnswer"
          class="approval-answer"
          rows="2"
          placeholder="也可以直接回答 Agent 的问题，它会据此调整…"
        ></textarea>
        <div class="approval-actions">
          <button class="secondary-button" @click="store.decideRunApproval(store.currentRun.id, false)">拒绝</button>
          <button
            v-if="approvalAnswer.trim()"
            class="secondary-button"
            @click="store.decideRunApproval(store.currentRun.id, false, approvalAnswer.trim())"
          >回答并继续</button>
          <button class="primary-button" @click="store.decideRunApproval(store.currentRun.id, true)">批准</button>
        </div>
      </section>

      <div v-if="thinking" class="thinking-row inline-thinking"><span></span><span></span><span></span><p>正在思考学习计划与下一步</p></div>

      <div v-if="displayAnswer" :class="['assistant-answer', { failed: finalEvent?.type === 'run.failed', streaming }]">
        <AgentMessage :content="displayAnswer" />
        <span v-if="streaming" class="stream-cursor" aria-hidden="true"></span>
        <small v-if="finalEvent?.type === 'run.failed'">错误编号：{{ finalEvent.payload?.code || 'run_failed' }}</small>
      </div>

      <PlanCard v-if="store.currentRun?.created_plan_id" :plan="store.planForRun(store.currentRun.id)" />
    </div>
  </div>
</template>
