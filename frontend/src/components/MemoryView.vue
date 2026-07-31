<script setup>
import { CheckIcon, ClockIcon, DocumentTextIcon, TrashIcon } from '@heroicons/vue/24/outline';
import { useWorkspaceStore } from '../stores/workspace';
import RunTraceButton from './RunTraceButton.vue';

const store = useWorkspaceStore();
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
        <article v-for="memory in store.memories" :key="memory.id" class="panel memory-card">
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
        <div v-if="!store.memories.length" class="panel empty-state"><ClockIcon />Agent 尚未提出长期记忆。它不会把临时推断偷偷写入画像。</div>
      </div>
    </div>
  </section>
</template>
