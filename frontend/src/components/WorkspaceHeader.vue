<script setup>
import {
  AdjustmentsHorizontalIcon,
  ArchiveBoxArrowDownIcon,
  EllipsisHorizontalIcon,
  FolderIcon,
  MapIcon,
  PencilSquareIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { usePlanStore } from '../stores/plan.js';
import { useSessionStore } from '../stores/session.js';
import { useShellStore } from '../stores/shell.js';

const planStore = usePlanStore();
const sessionStore = useSessionStore();
const shell = useShellStore();
const menuOpen = ref(false);
const title = computed(() => (
  planStore.focusedPlan?.title
  || sessionStore.activeSession?.title
  || 'Learning Agent'
));

async function archiveSession() {
  if (!sessionStore.activeSession) return;
  menuOpen.value = false;
  await shell.setSessionArchived(sessionStore.activeSession.id, true);
}
</script>

<template>
  <header class="workspace-header">
    <div class="workspace-title">
      <FolderIcon />
      <strong>{{ title }}</strong>
      <div class="workspace-menu-wrap">
        <button title="对话操作" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen"><EllipsisHorizontalIcon /></button>
        <div v-if="menuOpen" class="workspace-menu">
          <button @click="shell.traceOpen = true; menuOpen = false"><AdjustmentsHorizontalIcon /> 查看处理记录</button>
          <button @click="shell.startNewConversation(); menuOpen = false"><PencilSquareIcon /> 新对话</button>
          <button v-if="sessionStore.activeSession" @click="archiveSession"><ArchiveBoxArrowDownIcon /> 归档对话</button>
        </div>
      </div>
    </div>
    <div class="workspace-header-actions">
      <button v-if="planStore.focusedPlan" class="open-context-button" @click="shell.selectPlan(planStore.focusedPlan.id)">
        <MapIcon /> 打开计划
      </button>
      <button class="header-icon-button" title="查看本次对话的处理记录" @click="shell.traceOpen = true"><AdjustmentsHorizontalIcon /></button>
    </div>
  </header>
</template>
