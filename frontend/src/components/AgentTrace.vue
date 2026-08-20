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
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRunStore } from '../stores/run.js';
import { useShellStore } from '../stores/shell.js';
import { useSettingsStore } from '../stores/settings.js';
import { isRunBlocking, runStatusLabel as labelForRunStatus } from '../runState.js';

const runStore = useRunStore();
const settings = useSettingsStore();
const shell = useShellStore();
const panel = ref(null);
const expanded = ref(new Set());
const pendingUndoId = ref(null);
const running = computed(() => isRunBlocking(runStore.currentRun?.status));
const contextUsage = computed(() => {
  const event = [...runStore.runEvents].reverse().find((item) => item.type === 'context.built');
  if (!event?.payload?.estimated_tokens) return null;
  return {
    tokens: event.payload.estimated_tokens,
    window: settings.appSettings?.model_context_window || 128000,
  };
});
const undoable = computed(() => runStore.operations.filter(
  (operation) => operation.run_id === runStore.currentRun?.id && operation.status === 'committed',
));
const runStatusLabel = computed(() => labelForRunStatus(runStore.currentRun?.status));
let previouslyFocused = null;

function focusableElements() {
  if (!panel.value) return [];
  return [...panel.value.querySelectorAll(
    'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((element) => !element.hidden && element.getClientRects().length > 0);
}

function handleDialogKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault();
    shell.traceOpen = false;
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = focusableElements();
  if (!focusable.length) {
    event.preventDefault();
    panel.value?.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

onMounted(async () => {
  previouslyFocused = document.activeElement;
  document.addEventListener('keydown', handleDialogKeydown);
  await nextTick();
  const first = focusableElements()[0];
  if (first) first.focus();
  else panel.value?.focus();
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', handleDialogKeydown);
  if (previouslyFocused instanceof HTMLElement && previouslyFocused.isConnected) {
    previouslyFocused.focus();
  }
});

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

function eventExpandable(event) {
  return Boolean(
    event.type === 'context.built'
    || event.type?.startsWith('tool.')
    || event.type === 'run.budget_exceeded'
    || event.type?.startsWith('approval.'),
  );
}

function toggle(sequence) {
  const next = new Set(expanded.value);
  next.has(sequence) ? next.delete(sequence) : next.add(sequence);
  expanded.value = next;
}

async function confirmUndo(operation) {
  await runStore.undoOperation(operation.id);
  pendingUndoId.value = null;
}

function undoLabel(operation) {
  if (operation.tool_name === 'plan.create') return '撤销整个计划创建';
  if (operation.tool_name === 'quiz.grade') return '撤销评分与 XP';
  return `撤销 ${operation.tool_name}`;
}
</script>

<template>
  <aside
    ref="panel"
    class="trace-panel"
    role="dialog"
    aria-modal="true"
    aria-labelledby="trace-dialog-title"
    tabindex="-1"
  >
    <header class="trace-header">
      <div>
        <span class="eyebrow">本次对话</span>
        <h2 id="trace-dialog-title">处理记录</h2>
      </div>
      <div class="trace-header-actions">
        <span :class="['run-status', runStore.currentRun?.status || 'idle']">{{ runStatusLabel }}</span>
        <button class="trace-close" aria-label="关闭处理记录" @click="shell.traceOpen = false"><XMarkIcon /></button>
      </div>
    </header>

    <div v-if="runStore.currentRun" class="run-objective">
      <span>当前目标</span>
      <p>{{ runStore.currentRun.objective }}</p>
      <small>{{ triggerLabel(runStore.currentRun.trigger) }} · {{ runStore.currentRun.id.slice(0, 8) }}</small>
      <small v-if="runStore.currentRun.budget_usage" class="run-budget">
        模型调用 {{ runStore.currentRun.budget_usage.model_calls || 0 }} 次 · 工具 {{ runStore.currentRun.budget_usage.tool_calls || 0 }} 次
        <template v-if="runStore.currentRun.budget_usage.estimated_cost_usd"> · 约 ${{ Number(runStore.currentRun.budget_usage.estimated_cost_usd).toFixed(4) }}</template>
      </small>
      <small v-if="contextUsage" class="run-budget">
        上下文 ≈ {{ Math.round(contextUsage.tokens / 100) / 10 }}k / {{ Math.round(contextUsage.window / 1000) }}k tokens
      </small>
    </div>

    <div class="trace-list">
      <component
        :is="eventExpandable(event) ? 'button' : 'div'"
        v-for="event in runStore.runEvents"
        :key="event.sequence"
        :class="['trace-event', { expandable: eventExpandable(event) }]"
        :type="eventExpandable(event) ? 'button' : undefined"
        :aria-expanded="eventExpandable(event) ? expanded.has(event.sequence) : undefined"
        @click="eventExpandable(event) && toggle(event.sequence)"
      >
        <span :class="['event-icon', eventFailed(event) ? 'danger' : '']">
          <component :is="iconFor(event.type)" />
        </span>
        <span class="event-copy">
          <small>{{ eventTypeLabel(event.type) }}</small>
          <strong>{{ event.summary || '事件已记录' }}</strong>
          <pre v-if="eventExpandable(event) && expanded.has(event.sequence)">{{ JSON.stringify(event.payload, null, 2) }}</pre>
        </span>
        <ChevronRightIcon v-if="eventExpandable(event)" class="event-chevron" />
      </component>

      <div v-if="running" class="thinking-row">
        <span></span><span></span><span></span>
        <p>Agent 正在观察、规划并调用工具</p>
      </div>

      <div v-if="!runStore.currentRun" class="trace-empty">
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
          <div
            v-if="pendingUndoId === operation.id"
            class="undo-confirm"
            role="alertdialog"
            aria-label="确认撤销操作"
          >
            <p>将执行已记录的逆向操作，并保留审计记录。确定继续吗？</p>
            <span><button @click="pendingUndoId = null">取消</button><button class="confirm" @click="confirmUndo(operation)">确认撤销</button></span>
          </div>
        </div>
      </div>
      <button v-if="running" class="stop-button" @click="runStore.cancelCurrentRun">
        <StopIcon /> 停止处理
      </button>
      <p>展示行动摘要，不展示模型私有思维链</p>
    </footer>
  </aside>
</template>
