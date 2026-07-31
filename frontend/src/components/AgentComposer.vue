<script setup>
import { ArrowUpIcon, PlusIcon, SparklesIcon } from '@heroicons/vue/24/outline';
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
  <div class="composer-wrap">
    <div class="composer-shell">
      <button class="composer-plus" title="文件提交将在受限执行环境完成"><PlusIcon /></button>
      <textarea
        v-model="prompt"
        rows="1"
        placeholder="给 Learning Agent 发消息"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <span class="composer-mode"><SparklesIcon /> Hy3 · 深度</span>
      <button class="send-button" :disabled="!prompt.trim()" @click="submit"><ArrowUpIcon /></button>
    </div>
    <p>{{ store.currentPlan ? `当前关联：${store.currentPlan.title}` : 'Agent 可以读取上下文并调用工具执行动作' }}</p>
  </div>
</template>
