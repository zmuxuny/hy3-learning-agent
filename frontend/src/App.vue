<script setup>
import { AdjustmentsHorizontalIcon } from '@heroicons/vue/24/outline';
import { computed, onMounted } from 'vue';
import AgentTrace from './components/AgentTrace.vue';
import HomeView from './components/HomeView.vue';
import InboxView from './components/InboxView.vue';
import MemoryView from './components/MemoryView.vue';
import PlansView from './components/PlansView.vue';
import Sidebar from './components/Sidebar.vue';
import { useWorkspaceStore } from './stores/workspace';

const store = useWorkspaceStore();
const viewTitle = computed(() => ({
  home: store.currentRun?.objective || '新对话',
  plans: '学习计划',
  memory: 'AI 眼中的我',
  inbox: '主动消息',
}[store.activeView]));

onMounted(() => store.loadWorkspace());
</script>

<template>
  <div class="app-shell">
    <Sidebar />
    <main class="workspace">
      <header class="workspace-bar">
        <strong>{{ viewTitle }}</strong>
        <button class="trace-toggle" @click="store.traceOpen = true">
          <span v-if="store.currentRun" :class="['status-dot', store.currentRun.status]"></span>
          <AdjustmentsHorizontalIcon />
          <span>运行详情</span>
        </button>
      </header>
      <div v-if="store.loading" class="page-loader"><span></span><p>正在恢复学习上下文…</p></div>
      <template v-else>
        <HomeView v-if="store.activeView === 'home'" />
        <PlansView v-else-if="store.activeView === 'plans'" />
        <MemoryView v-else-if="store.activeView === 'memory'" />
        <InboxView v-else-if="store.activeView === 'inbox'" />
      </template>
    </main>
    <button v-if="store.traceOpen" class="trace-backdrop" aria-label="关闭运行详情" @click="store.traceOpen = false"></button>
    <AgentTrace v-if="store.traceOpen" />
  </div>
</template>
