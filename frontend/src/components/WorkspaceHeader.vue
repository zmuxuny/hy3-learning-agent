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
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const menuOpen = ref(false);
const title = computed(() => (
  store.focusedPlan?.title
  || store.activeSession?.title
  || 'Learning Agent'
));

async function archiveSession() {
  if (!store.activeSession) return;
  menuOpen.value = false;
  await store.setSessionArchived(store.activeSession.id, true);
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
          <button @click="store.traceOpen = true; menuOpen = false"><AdjustmentsHorizontalIcon /> 运行与上下文</button>
          <button @click="store.startNewConversation(); menuOpen = false"><PencilSquareIcon /> 新对话</button>
          <button v-if="store.activeSession" @click="archiveSession"><ArchiveBoxArrowDownIcon /> 归档对话</button>
        </div>
      </div>
    </div>
    <div class="workspace-header-actions">
      <button v-if="store.focusedPlan" class="open-context-button" @click="store.selectPlan(store.focusedPlan.id)">
        <MapIcon /> 打开计划
      </button>
      <button class="header-icon-button" title="运行与上下文详情" @click="store.traceOpen = true"><AdjustmentsHorizontalIcon /></button>
    </div>
  </header>
</template>
