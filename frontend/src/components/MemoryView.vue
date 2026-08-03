<script setup>
import { CheckIcon, ClockIcon, DocumentTextIcon, MagnifyingGlassIcon, TrashIcon } from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import RunTraceButton from './RunTraceButton.vue';

const store = useWorkspaceStore();
const search = ref('');
const filteredMemories = computed(() => {
  const query = search.value.trim().toLowerCase();
  if (!query) return store.memories;
  return store.memories.filter((memory) => (
    `${memory.content} ${memory.scope} ${memory.layer} ${memory.status}`.toLowerCase().includes(query)
  ));
});
const confirmedCount = computed(() => store.memories.filter((memory) => memory.status === 'confirmed').length);
const proposedCount = computed(() => store.memories.filter((memory) => memory.status === 'proposed').length);
const scopeCounts = computed(() => ({
  global: store.memories.filter((memory) => memory.scope === 'global').length,
  plan: store.memories.filter((memory) => memory.scope === 'plan').length,
  session: store.memories.filter((memory) => memory.scope === 'session').length,
}));
</script>

<template>
  <section class="view">
    <header class="view-header compact-header">
      <div><span class="eyebrow">MEMORY INSPECTOR</span><h1>AI 眼中的我</h1><p>每条长期认识都带有来源、范围和确认状态；你始终拥有纠正和删除权。</p></div>
      <div class="page-header-actions">
        <RunTraceButton />
        <span class="memory-count">{{ store.memories.length }} 条记忆</span>
      </div>
    </header>

    <div class="memory-layout">
      <article class="panel memory-summary">
        <div class="memory-avatar">AI</div>
        <h2>自主学习 Agent</h2>
        <p>当前画像会被所有计划安全引用，计划私有内容不会自动提升为全局记忆。</p>
        <dl v-if="store.profile">
          <div><dt>协作方式</dt><dd>{{ store.profile.agent_style }}</dd></div>
          <div><dt>免打扰</dt><dd>{{ store.profile.quiet_hours.start }}—{{ store.profile.quiet_hours.end }}</dd></div>
          <div><dt>通知上限</dt><dd>{{ store.profile.daily_notification_limit }}/天</dd></div>
        </dl>
      </article>

      <div class="memory-list">
        <div class="memory-toolbar">
          <label class="memory-search"><MagnifyingGlassIcon /><input v-model="search" placeholder="搜索记忆内容、作用域或层级" /></label>
          <span class="memory-filter-counts">
            <em>确认 {{ confirmedCount }}</em><em>待确认 {{ proposedCount }}</em><em>全局 {{ scopeCounts.global }}</em><em>计划 {{ scopeCounts.plan }}</em><em>会话 {{ scopeCounts.session }}</em>
          </span>
        </div>
        <article v-for="memory in filteredMemories" :key="memory.id" class="panel memory-card">
          <div class="memory-icon"><DocumentTextIcon /></div>
          <div class="memory-body">
            <header><span>{{ memory.scope }} / {{ memory.layer }}</span><em :class="memory.status">{{ memory.status }}</em></header>
            <p>{{ memory.content }}</p>
            <footer><small>来源：{{ memory.source_type }}{{ memory.source_id ? ` · ${memory.source_id}` : '' }}</small><small>置信度 {{ Math.round(memory.confidence * 100) }}%</small></footer>
          </div>
          <div class="memory-actions">
            <button v-if="memory.status === 'proposed'" title="确认长期记忆" @click="store.confirmMemory(memory.id)"><CheckIcon /></button>
            <button title="删除记忆" @click="store.deleteMemory(memory.id)"><TrashIcon /></button>
          </div>
        </article>
        <div v-if="!filteredMemories.length" class="panel empty-state"><ClockIcon />{{ search ? '没有匹配的记忆。' : 'Agent 尚未提出长期记忆。它不会把临时推断偷偷写入画像。' }}</div>
      </div>
    </div>
  </section>
</template>
