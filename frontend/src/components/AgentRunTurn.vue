<script setup>
import {
  CheckCircleIcon,
  ArrowRightIcon,
  CircleStackIcon,
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
});
const store = useWorkspaceStore();
const processExpanded = ref(false);
const running = computed(() => ['queued', 'running'].includes(store.currentRun?.status));
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
const visibleActivityEvents = computed(() => (
  processExpanded.value ? activityEvents.value : activityEvents.value.slice(0, 6)
));
const finalEvent = computed(() => [...store.runEvents].reverse().find(
  (event) => ['assistant.message', 'run.completed', 'run.failed', 'run.cancelled'].includes(event.type),
));
const answerText = computed(() => props.answer || finalEvent.value?.summary || '');

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
</script>

<template>
  <div class="agent-turn run-turn">
    <div class="agent-avatar"><SparklesIcon /></div>
    <div class="agent-content">
      <div class="agent-name">Learning Agent <span>Hy3</span></div>

      <div v-if="activityEvents.length || running" class="inline-process">
        <div
          v-for="event in visibleActivityEvents"
          :key="event.sequence"
          :class="['process-row', { failed: event.type === 'run.failed' || event.type === 'run.cancelled' || event.payload?.result?.ok === false }]"
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
