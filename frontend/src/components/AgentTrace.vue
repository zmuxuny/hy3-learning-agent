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
const running = computed(() => ['queued', 'running'].includes(store.currentRun?.status));
const undoable = computed(() => store.operations.filter(
  (operation) => operation.run_id === store.currentRun?.id && operation.status === 'committed',
));

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
        <span class="eyebrow">AGENT HARNESS</span>
        <h2>运行轨迹</h2>
      </div>
      <div class="trace-header-actions">
        <span :class="['run-status', store.currentRun?.status || 'idle']">{{ store.currentRun?.status || 'idle' }}</span>
        <button class="trace-close" @click="store.traceOpen = false"><XMarkIcon /></button>
      </div>
    </header>

    <div v-if="store.currentRun" class="run-objective">
      <span>当前目标</span>
      <p>{{ store.currentRun.objective }}</p>
      <small>{{ store.currentRun.trigger }} · {{ store.currentRun.id.slice(0, 8) }}</small>
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
          <small>{{ event.type }}</small>
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
        <strong>还没有运行记录</strong>
        <p>向 Agent 提出目标后，这里会实时展示上下文、工具和操作结果。</p>
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
        <StopIcon /> 停止运行
      </button>
      <p>展示行动摘要，不展示模型私有思维链</p>
    </footer>
  </aside>
</template>
