<script setup>
import { BellIcon, XMarkIcon } from '@heroicons/vue/24/outline';
import { onBeforeUnmount, onMounted } from 'vue';
import AgentTrace from './components/AgentTrace.vue';
import HomeView from './components/HomeView.vue';
import InboxView from './components/InboxView.vue';
import MemoryView from './components/MemoryView.vue';
import PlansView from './components/PlansView.vue';
import SettingsView from './components/SettingsView.vue';
import Sidebar from './components/Sidebar.vue';
import { useWorkspaceStore } from './stores/workspace';

const store = useWorkspaceStore();
onMounted(async () => {
  await store.loadWorkspace();
  store.startProactiveSync();
  const params = new URLSearchParams(window.location.search);
  if (params.get('view') === 'inbox') store.openView('inbox');
});
onBeforeUnmount(() => store.stopProactiveSync());
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
        <SettingsView v-else-if="store.activeView === 'settings'" />
      </template>
    </main>
    <button v-if="store.traceOpen" class="trace-backdrop" aria-label="关闭运行详情" @click="store.traceOpen = false"></button>
    <AgentTrace v-if="store.traceOpen" />
    <aside v-if="store.proactiveNotice" class="proactive-toast" aria-live="polite">
      <BellIcon />
      <button class="proactive-toast-copy" @click="store.openView('inbox'); store.dismissProactiveNotice()">
        <small>Learning Agent 主动消息</small>
        <strong>{{ store.proactiveNotice.title }}</strong>
        <span>{{ store.proactiveNotice.body }}</span>
      </button>
      <button class="proactive-toast-close" aria-label="关闭通知" @click="store.dismissProactiveNotice"><XMarkIcon /></button>
    </aside>
  </div>
</template>
