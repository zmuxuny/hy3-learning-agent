<script setup>
import {
  ArrowUturnLeftIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  CircleStackIcon,
  CommandLineIcon,
  StopIcon,
  XMarkIcon,
  XCircleIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const expanded = ref(new Set());
const pendingUndoId = ref(null);
const running = computed(() => ['queued', 'running', 'waiting_approval'].includes(store.currentRun?.status));
const contextUsage = computed(() => {
  const event = [...store.runEvents].reverse().find((item) => item.type === 'context.built');
  if (!event?.payload?.estimated_tokens) return null;
  return {
    tokens: event.payload.estimated_tokens,
    window: store.appSettings?.model_context_window || 128000,
  };
});
const undoable = computed(() => store.operations.filter(
  (operation) => operation.run_id === store.currentRun?.id && operation.status === 'committed',
));
const runStatusLabel = computed(() => ({
  queued: '等待处理',
  running: '处理中',
  waiting_approval: '等待确认',
  completed: '已完成',
  failed: '失败',
  cancelled: '已停止',
}[store.currentRun?.status] || '空闲'));

function triggerLabel(trigger) {
  return {
    user_message: '用户消息',
    heartbeat: '主动检查',
    email_reply: '邮件回复',
    subagent: '子 Agent 调查',
  }[trigger] || trigger || '未知来源';
}

function eventTypeLabel(type) {
  if (type === 'context.built') return '上下文';
  if (type?.startsWith('tool.')) return '工具操作';
  if (type?.startsWith('subagent.')) return '子 Agent';
  if (type?.startsWith('approval.')) return '用户确认';
  if (type?.startsWith('assistant.')) return 'Agent 更新';
  if (type?.startsWith('run.')) return '处理状态';
  return '操作记录';
}

function iconFor(type) {
  if (type === 'tool.started' || type === 'tool.completed') return CommandLineIcon;
  if (type === 'context.built') return CircleStackIcon;
  if (type === 'run.failed') return XCircleIcon;
  return CheckCircleIcon;
}

function eventFailed(event) {
  return event.type.includes('failed') || event.payload?.result?.ok === false;
}

function toggle(sequence) {
  const next = new Set(expanded.value);
  next.has(sequence) ? next.delete(sequence) : next.add(sequence);
  expanded.value = next;
}

async function confirmUndo(operation) {
  await store.undoOperation(operation.id);
  pendingUndoId.value = null;
}

function undoLabel(operation) {
  if (operation.tool_name === 'plan.create') return '撤销整个计划创建';
  if (operation.tool_name === 'quiz.grade') return '撤销评分与 XP';
  return `撤销 ${operation.tool_name}`;
}
</script>

<template>
  <aside class="trace-panel">
    <header class="trace-header">
      <div>
        <span class="eyebrow">本次对话</span>
        <h2>处理记录</h2>
      </div>
      <div class="trace-header-actions">
        <span :class="['run-status', store.currentRun?.status || 'idle']">{{ runStatusLabel }}</span>
        <button class="trace-close" @click="store.traceOpen = false"><XMarkIcon /></button>
      </div>
    </header>

    <div v-if="store.currentRun" class="run-objective">
      <span>当前目标</span>
      <p>{{ store.currentRun.objective }}</p>
      <small>{{ triggerLabel(store.currentRun.trigger) }} · {{ store.currentRun.id.slice(0, 8) }}</small>
      <small v-if="store.currentRun.budget_usage" class="run-budget">
        模型调用 {{ store.currentRun.budget_usage.model_calls || 0 }} 次 · 工具 {{ store.currentRun.budget_usage.tool_calls || 0 }} 次
        <template v-if="store.currentRun.budget_usage.estimated_cost_usd"> · 约 ${{ Number(store.currentRun.budget_usage.estimated_cost_usd).toFixed(4) }}</template>
      </small>
      <small v-if="contextUsage" class="run-budget">
        上下文 ≈ {{ Math.round(contextUsage.tokens / 100) / 10 }}k / {{ Math.round(contextUsage.window / 1000) }}k tokens
      </small>
    </div>

    <div class="trace-list">
      <button
        v-for="event in store.runEvents"
        :key="event.sequence"
        class="trace-event"
        @click="toggle(event.sequence)"
      >
        <span :class="['event-icon', eventFailed(event) ? 'danger' : '']">
          <component :is="iconFor(event.type)" />
        </span>
        <span class="event-copy">
          <small>{{ eventTypeLabel(event.type) }}</small>
          <strong>{{ event.summary || '事件已记录' }}</strong>
          <pre v-if="expanded.has(event.sequence)">{{ JSON.stringify(event.payload, null, 2) }}</pre>
        </span>
        <ChevronRightIcon class="event-chevron" />
      </button>

      <div v-if="running" class="thinking-row">
        <span></span><span></span><span></span>
        <p>Agent 正在观察、规划并调用工具</p>
      </div>

      <div v-if="!store.currentRun" class="trace-empty">
        <CommandLineIcon />
        <strong>还没有处理记录</strong>
        <p>提出学习目标后，这里会实时展示读取的上下文、使用的工具和操作结果。</p>
      </div>
    </div>

    <footer class="trace-footer">
      <div v-if="undoable.length" class="undo-list">
        <span>本次运行的可撤销操作</span>
        <div v-for="operation in undoable" :key="operation.id" class="undo-item">
          <button @click="pendingUndoId = operation.id"><ArrowUturnLeftIcon /> {{ undoLabel(operation) }}</button>
          <div v-if="pendingUndoId === operation.id" class="undo-confirm">
            <p>将执行已记录的逆向操作，并保留审计记录。确定继续吗？</p>
            <span><button @click="pendingUndoId = null">取消</button><button class="confirm" @click="confirmUndo(operation)">确认撤销</button></span>
          </div>
        </div>
      </div>
      <button v-if="running" class="stop-button" @click="store.cancelCurrentRun">
        <StopIcon /> 停止处理
      </button>
      <p>展示行动摘要，不展示模型私有思维链</p>
    </footer>
  </aside>
</template>
