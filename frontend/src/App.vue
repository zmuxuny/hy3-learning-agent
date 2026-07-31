<script setup>
import { onBeforeUnmount, onMounted } from 'vue';
import AgentTrace from './components/AgentTrace.vue';
import HomeView from './components/HomeView.vue';
import InboxView from './components/InboxView.vue';
import MemoryView from './components/MemoryView.vue';
import PlansView from './components/PlansView.vue';
import Sidebar from './components/Sidebar.vue';
import { useWorkspaceStore } from './stores/workspace';

const store = useWorkspaceStore();

onMounted(() => store.loadWorkspace());
onBeforeUnmount(() => {});
</script>

<template>
  <div class="app-shell">
    <Sidebar />
    <main class="workspace">
      <div v-if="store.loading" class="page-loader"><span></span><p>正在恢复学习上下文…</p></div>
      <template v-else>
        <HomeView v-if="store.activeView === 'home'" />
        <PlansView v-else-if="store.activeView === 'plans'" />
        <MemoryView v-else-if="store.activeView === 'memory'" />
        <InboxView v-else-if="store.activeView === 'inbox'" />
      </template>
    </main>
    <AgentTrace />
  </div>
</template>
