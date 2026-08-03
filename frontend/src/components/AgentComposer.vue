<script setup>
import { ArrowUpIcon, MapIcon, PlusIcon, SparklesIcon, StopIcon } from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import api from '../api/client';

const store = useWorkspaceStore();
const prompt = ref('');
const fileInput = ref(null);
const uploading = ref(false);
const queueMenuOpen = ref(false);
const running = computed(() => ['queued', 'running', 'waiting_approval'].includes(store.currentRun?.status));
const queued = computed(() => store.pendingQueuedObjective);
const contextUsage = computed(() => {
  const event = [...store.runEvents].reverse().find((item) => item.type === 'context.built');
  if (!event?.payload?.estimated_tokens) return null;
  return {
    tokens: event.payload.estimated_tokens,
    window: store.appSettings?.model_context_window || 128000,
  };
});
const contextLabel = computed(() => (
  contextUsage.value ? `${Math.round(contextUsage.value.tokens / 1000)}k / ${Math.round(contextUsage.value.window / 1000)}k` : ''
));
const archivedContext = computed(() => (
  Boolean(store.activeSession?.archived_at) || store.focusedPlan?.status === 'archived'
));

async function submit() {
  if (archivedContext.value) return;
  const value = prompt.value.trim();
  if (!value) return;
  if (running.value) {
    queueMenuOpen.value = true;
    return;
  }
  await sendNow(value);
}

async function sendNow(value, mode = 'normal') {
  const started = await store.startRun(value, undefined, mode === 'interrupt' ? { mode: 'interrupt' } : {});
  if (started) prompt.value = '';
}

async function queueSend() {
  const value = prompt.value.trim();
  queueMenuOpen.value = false;
  if (!value) return;
  const started = await store.startRun(value, undefined, { mode: 'queue' });
  if (started) prompt.value = '';
}

async function interruptSend() {
  const value = prompt.value.trim();
  queueMenuOpen.value = false;
  if (!value) return;
  await sendNow(value, 'interrupt');
}

function stopRun() {
  store.cancelCurrentRun();
}

async function uploadFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  uploading.value = true;
  try {
    const response = await api.upload('/workspace/files', file);
    prompt.value = `请读取并检查我上传的学习成果文件 \`${response.data.path}\`。如果当前对话聚焦某个任务，请把它作为 submission_create 的证据；需要时运行代码或测试，再用 submission_check 给出验收结果。`;
  } catch (uploadError) {
    store.error = uploadError.message;
  } finally {
    uploading.value = false;
    event.target.value = '';
  }
}

async function switchSession(event) {
  const sessionId = event.target.value;
  if (!sessionId) {
    store.startNewConversation();
    return;
  }
  const session = store.sessions.find((item) => item.id === sessionId);
  if (session) await store.selectSession(session);
}
</script>

<template>
  <div class="composer-wrap">
    <div class="composer-shell">
      <button class="composer-plus" :disabled="running || uploading || archivedContext" title="上传学习成果" @click="fileInput.click()"><PlusIcon /></button>
      <input ref="fileInput" class="visually-hidden" type="file" @change="uploadFile" />
      <textarea
        v-model="prompt"
        rows="1"
        :placeholder="archivedContext ? '归档内容为只读，恢复后可以继续对话' : uploading ? '正在上传文件…' : running ? (queued ? '已排队，等待当前运行结束…' : 'Agent 正在运行，可发送消息排队或打断') : '给 Learning Agent 发消息'"
        :disabled="archivedContext"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <span class="composer-mode"><SparklesIcon /> Hy3 · 深度</span>
      <div class="composer-actions">
        <button v-if="running" class="composer-stop" title="停止当前运行" @click="stopRun"><StopIcon /></button>
        <button class="send-button" :class="{ running }" :disabled="archivedContext || !prompt.trim()" :title="running ? '运行中：选择排队或打断' : '发送'" @click="submit"><ArrowUpIcon /></button>
      </div>
    </div>
    <div v-if="queueMenuOpen" class="queue-menu">
      <button @click="queueSend">排队等待当前运行结束</button>
      <button class="danger" @click="interruptSend">打断当前运行并发送</button>
      <button class="quiet" @click="queueMenuOpen = false">取消</button>
    </div>
    <div v-if="queued" class="queued-chip">
      <span>已排队：{{ queued.objective.length > 40 ? `${queued.objective.slice(0, 40)}…` : queued.objective }}</span>
      <button @click="store.clearQueued()">取消排队</button>
    </div>
    <div :class="['composer-context', { focused: store.focusedPlan }]">
      <span v-if="store.focusedPlan">
        <MapIcon /><strong>{{ store.focusedPlan.status === 'archived' ? '已归档计划' : '计划焦点' }}</strong>{{ store.focusedPlan.title }}
      </span>
      <span v-else>
        <SparklesIcon /><strong>全局对话</strong>Agent 可以协调所有计划
      </span>
      <small v-if="contextUsage" class="context-usage" :title="`上下文约 ${contextUsage.tokens.toLocaleString()} / ${contextUsage.window.toLocaleString()} tokens`">上下文 {{ contextLabel }}</small>
      <button v-if="store.focusedPlan && store.activeView === 'home'" @click="store.startNewConversation">
        开始全局对话
      </button>
      <label class="mobile-session-switch">
        <span class="visually-hidden">切换对话</span>
        <select :value="store.activeSessionId || ''" @change="switchSession">
          <option value="">＋ 新对话</option>
          <option v-for="session in store.sessions" :key="session.id" :value="session.id">
            {{ session.title }}
          </option>
        </select>
      </label>
    </div>
  </div>
</template>
