<script setup>
import { ArrowUpIcon, MapIcon, PlusIcon, SparklesIcon } from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const prompt = ref('');
const running = computed(() => ['queued', 'running'].includes(store.currentRun?.status));

async function submit() {
  if (running.value) return;
  const value = prompt.value.trim();
  if (!value) return;
  const started = await store.startRun(value);
  if (started) prompt.value = '';
}
</script>

<template>
  <div class="composer-wrap">
    <div class="composer-shell">
      <button class="composer-plus" title="文件提交将在受限执行环境完成"><PlusIcon /></button>
      <textarea
        v-model="prompt"
        rows="1"
        :placeholder="running ? 'Agent 正在运行，可以在运行详情中停止' : '给 Learning Agent 发消息'"
        :disabled="running"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <span class="composer-mode"><SparklesIcon /> Hy3 · 深度</span>
      <button class="send-button" :disabled="running || !prompt.trim()" @click="submit"><ArrowUpIcon /></button>
    </div>
    <div :class="['composer-context', { focused: store.focusedPlan }]">
      <span v-if="store.focusedPlan">
        <MapIcon /><strong>计划焦点</strong>{{ store.focusedPlan.title }}
      </span>
      <span v-else>
        <SparklesIcon /><strong>全局对话</strong>Agent 可以协调所有计划
      </span>
      <button v-if="store.focusedPlan && store.activeView === 'home'" @click="store.startNewConversation">
        开始全局对话
      </button>
    </div>
  </div>
</template>
