<script setup>
import { ArrowUpIcon, PaperClipIcon, SparklesIcon } from '@heroicons/vue/24/outline';
import { ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const prompt = ref('');

async function submit() {
  const value = prompt.value.trim();
  if (!value) return;
  prompt.value = '';
  await store.startRun(value, store.currentPlan?.id || null);
}
</script>

<template>
  <div class="composer-shell">
    <div class="composer-context">
      <SparklesIcon />
      <span>{{ store.currentPlan ? `当前计划：${store.currentPlan.title}` : '全局学习 Agent' }}</span>
    </div>
    <textarea
      v-model="prompt"
      rows="2"
      placeholder="告诉 Agent 你的目标，或让它检查、调整、提醒和考核……"
      @keydown.enter.exact.prevent="submit"
    ></textarea>
    <div class="composer-actions">
      <button class="icon-button" title="文件工具将在考核流程中启用"><PaperClipIcon /></button>
      <span>Enter 发送 · Agent 可以调用工具执行动作</span>
      <button class="send-button" @click="submit"><ArrowUpIcon /></button>
    </div>
  </div>
</template>
