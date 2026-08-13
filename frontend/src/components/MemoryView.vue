<script setup>
import {
  ArchiveBoxIcon,
  ArrowPathIcon,
  CheckIcon,
  ClockIcon,
  DocumentTextIcon,
  MagnifyingGlassIcon,
  PencilSquareIcon,
  XMarkIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import RunTraceButton from './RunTraceButton.vue';

const store = useWorkspaceStore();
const search = ref('');
const memoryTab = ref('active');
const editingMemoryId = ref(null);
const correctionText = ref('');
const submittingCorrection = ref(false);
const historicalStatuses = new Set(['archived', 'expired', 'superseded']);
const lifecycleMemories = computed(() => store.memories.filter((memory) => (
  (memoryTab.value === 'history') === historicalStatuses.has(memory.status)
)));
const filteredMemories = computed(() => {
  const query = search.value.trim().toLowerCase();
  return lifecycleMemories.value.filter((memory) => !query || (
    `${memory.content} ${memory.scope} ${memory.layer} ${memory.status}`.toLowerCase().includes(query)
  ));
});
const confirmedCount = computed(() => store.memories.filter((memory) => memory.status === 'confirmed').length);
const proposedCount = computed(() => store.memories.filter((memory) => memory.status === 'proposed').length);
const historyCount = computed(() => store.memories.filter((memory) => historicalStatuses.has(memory.status)).length);
const scopeCounts = computed(() => ({
  global: lifecycleMemories.value.filter((memory) => memory.scope === 'global').length,
  plan: lifecycleMemories.value.filter((memory) => memory.scope === 'plan').length,
  session: lifecycleMemories.value.filter((memory) => memory.scope === 'session').length,
}));

const scopeLabel = (scope) => ({ global: '全局', plan: '计划', session: '会话' }[scope] || scope);
const layerLabel = (layer) => ({
  semantic: '稳定认识', long_term: '长期', episodic: '情节', short_term: '短期', working: '工作',
}[layer] || layer);
const statusLabel = (status) => ({
  proposed: '待确认', confirmed: '已确认', archived: '已归档', expired: '已到期', superseded: '已替代',
}[status] || status);
const formatTime = (value) => (value ? new Intl.DateTimeFormat('zh-CN', {
  month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
}).format(new Date(value)) : '尚未用于检索');

function beginCorrection(memory) {
  editingMemoryId.value = memory.id;
  correctionText.value = memory.content;
}

function cancelCorrection() {
  editingMemoryId.value = null;
  correctionText.value = '';
}

async function submitCorrection(memory) {
  if (!correctionText.value.trim() || correctionText.value.trim() === memory.content.trim()) return;
  submittingCorrection.value = true;
  try {
    if (await store.proposeMemoryCorrection(memory.id, correctionText.value)) cancelCorrection();
  } finally {
    submittingCorrection.value = false;
  }
}
</script>

<template>
  <section class="view">
    <header class="view-header compact-header">
      <div><span class="eyebrow">MEMORY INSPECTOR</span><h1>AI 眼中的我</h1><p>查看 Agent 会带入上下文的认识，并追溯它何时被使用、纠正或归档。</p></div>
      <div class="page-header-actions">
        <RunTraceButton />
        <span class="memory-count">{{ store.memories.length }} 条记忆</span>
      </div>
    </header>


    <div class="memory-layout">
      <article class="panel memory-summary">
        <div class="memory-avatar">AI</div>
        <h2>自主学习 Agent</h2>
        <p>全局认识可跨计划使用；计划与会话记忆保持隔离。纠正旧认识前，需要你确认新的版本。</p>
        <dl v-if="store.profile">
          <div><dt>协作方式</dt><dd>{{ store.profile.agent_style }}</dd></div>
          <div><dt>免打扰</dt><dd>{{ store.profile.quiet_hours.start }}—{{ store.profile.quiet_hours.end }}</dd></div>
          <div><dt>通知上限</dt><dd>{{ store.profile.daily_notification_limit }}/天</dd></div>
        </dl>
      </article>

      <div class="memory-list">
        <nav class="memory-tabs" aria-label="记忆状态">
          <button :class="{ active: memoryTab === 'active' }" @click="memoryTab = 'active'">当前 <span>{{ confirmedCount + proposedCount }}</span></button>
          <button :class="{ active: memoryTab === 'history' }" @click="memoryTab = 'history'">历史 <span>{{ historyCount }}</span></button>
        </nav>
        <div class="memory-toolbar">
          <label class="memory-search"><MagnifyingGlassIcon /><input v-model="search" placeholder="搜索记忆内容、作用域或层级" /></label>
          <span class="memory-filter-counts">
            <em>确认 {{ confirmedCount }}</em><em>待确认 {{ proposedCount }}</em><em>全局 {{ scopeCounts.global }}</em><em>计划 {{ scopeCounts.plan }}</em><em>会话 {{ scopeCounts.session }}</em>
          </span>
        </div>
        <article v-for="memory in filteredMemories" :key="memory.id" class="panel memory-card" :class="{ historical: historicalStatuses.has(memory.status) }">
          <div class="memory-icon"><DocumentTextIcon /></div>
          <div class="memory-body">
            <header><span>{{ scopeLabel(memory.scope) }} · {{ layerLabel(memory.layer) }}</span><em :class="memory.status">{{ statusLabel(memory.status) }}</em></header>
            <p v-if="editingMemoryId !== memory.id">{{ memory.content }}</p>
            <form v-else class="memory-correction" @submit.prevent="submitCorrection(memory)">
              <label :for="`memory-correction-${memory.id}`">修正后的认识</label>
              <textarea :id="`memory-correction-${memory.id}`" v-model="correctionText" rows="3" autofocus />
              <div><button type="button" @click="cancelCorrection"><XMarkIcon />取消</button><button type="submit" class="primary" :disabled="submittingCorrection || correctionText.trim() === memory.content.trim()"><CheckIcon />提交确认</button></div>
            </form>
            <p v-if="memory.archived_reason" class="memory-history-reason">{{ memory.archived_reason }}</p>
            <footer>
              <small>来源：{{ memory.source_type }}{{ memory.source_id ? ` · ${memory.source_id}` : '' }}</small>
              <small>使用 {{ memory.access_count }} 次 · {{ formatTime(memory.last_accessed_at) }}</small>
              <small>置信度 {{ Math.round(memory.confidence * 100) }}%</small>
            </footer>
          </div>
          <div class="memory-actions">
            <button v-if="memory.status === 'proposed'" type="button" aria-label="确认长期记忆" title="确认长期记忆" @click="store.confirmMemory(memory.id)"><CheckIcon /></button>
            <button v-if="memory.status === 'confirmed'" type="button" aria-label="纠正这条记忆" title="纠正这条记忆" @click="beginCorrection(memory)"><PencilSquareIcon /></button>
            <button v-if="['proposed', 'confirmed'].includes(memory.status)" type="button" aria-label="归档记忆" title="归档记忆" @click="store.archiveMemory(memory.id)"><ArchiveBoxIcon /></button>
            <button v-if="memory.restorable" type="button" aria-label="恢复记忆" title="恢复记忆" @click="store.restoreMemory(memory.id)"><ArrowPathIcon /></button>
          </div>
        </article>
        <div v-if="!filteredMemories.length" class="panel empty-state"><ClockIcon />{{ search ? '没有匹配的记忆。' : memoryTab === 'history' ? '还没有归档、到期或被替代的记忆。' : 'Agent 尚未提出长期记忆。它不会把临时推断偷偷写入画像。' }}</div>
      </div>
    </div>
  </section>
</template>
