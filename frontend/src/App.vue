<script setup>
import { BellIcon, ExclamationTriangleIcon, XMarkIcon } from '@heroicons/vue/24/outline';
import { onBeforeUnmount, onMounted, watch } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import AgentTrace from './components/AgentTrace.vue';
import Sidebar from './components/Sidebar.vue';
import WorkspaceHeader from './components/WorkspaceHeader.vue';
import { useInboxStore } from './stores/inbox.js';
import { useShellStore } from './stores/shell.js';
import { useSettingsStore } from './stores/settings.js';

const route = useRoute();
const inbox = useInboxStore();
const shell = useShellStore();
const settings = useSettingsStore();
onMounted(async () => {
  shell.configureRunEvents();
  await shell.bootstrap();
  inbox.startProactiveSync(settings.loadSchedulerStatus);
});
watch(() => route.fullPath, () => shell.hydrateRoute(route));
onBeforeUnmount(() => inbox.stopProactiveSync());
</script>

<template>
  <div class="app-shell">
    <Sidebar />
    <main class="workspace">
      <WorkspaceHeader v-if="!shell.coreLoading && ['home', 'session', 'inbox-intervention'].includes(route.name)" />
      <div v-if="shell.coreLoading" class="page-loader"><span></span><p>正在恢复学习上下文…</p></div>
      <RouterView v-else />
    </main>
    <button v-if="shell.traceOpen" class="trace-backdrop" aria-label="关闭运行详情" @click="shell.traceOpen = false"></button>
    <AgentTrace v-if="shell.traceOpen" />
    <aside v-if="shell.error" class="app-error-toast" role="alert">
      <ExclamationTriangleIcon />
      <span>{{ shell.error }}</span>
      <button aria-label="关闭错误提示" @click="shell.error = ''"><XMarkIcon /></button>
    </aside>
    <aside v-if="inbox.proactiveNotice" :class="['proactive-toast', { shifted: shell.error }]" aria-live="polite">
      <BellIcon />
      <button class="proactive-toast-copy" @click="shell.openNotification(inbox.proactiveNotice)">
        <small>学习进度提醒</small>
        <strong>{{ inbox.proactiveNotice.title }}</strong>
        <span>{{ inbox.proactiveNotice.body }}</span>
      </button>
      <button class="proactive-toast-close" aria-label="关闭通知" @click="inbox.dismissProactiveNotice"><XMarkIcon /></button>
    </aside>
  </div>
</template>
