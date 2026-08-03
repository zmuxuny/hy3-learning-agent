<script setup>
import { ArrowUpIcon, MapIcon, PlusIcon, SparklesIcon } from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import api from '../api/client';

const store = useWorkspaceStore();
const prompt = ref('');
const fileInput = ref(null);
const uploading = ref(false);
const running = computed(() => ['queued', 'running', 'waiting_approval'].includes(store.currentRun?.status));
const archivedContext = computed(() => (
  Boolean(store.activeSession?.archived_at) || store.focusedPlan?.status === 'archived'
));

async function submit() {
  if (running.value || archivedContext.value) return;
  const value = prompt.value.trim();
  if (!value) return;
  const started = await store.startRun(value);
  if (started) prompt.value = '';
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
        :placeholder="archivedContext ? '归档内容为只读，恢复后可以继续对话' : uploading ? '正在上传文件…' : running ? 'Agent 正在运行，可以在运行详情中停止' : '给 Learning Agent 发消息'"
        :disabled="running || archivedContext"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <span class="composer-mode"><SparklesIcon /> Hy3 · 深度</span>
      <button class="send-button" :disabled="running || archivedContext || !prompt.trim()" @click="submit"><ArrowUpIcon /></button>
    </div>
    <div :class="['composer-context', { focused: store.focusedPlan }]">
      <span v-if="store.focusedPlan">
        <MapIcon /><strong>{{ store.focusedPlan.status === 'archived' ? '已归档计划' : '计划焦点' }}</strong>{{ store.focusedPlan.title }}
      </span>
      <span v-else>
        <SparklesIcon /><strong>全局对话</strong>Agent 可以协调所有计划
      </span>
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
