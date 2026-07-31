<script setup>
import { AdjustmentsHorizontalIcon } from '@heroicons/vue/24/outline';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
</script>

<template>
  <button class="trace-toggle" @click="store.traceOpen = true">
    <span v-if="store.currentRun" :class="['status-dot', store.currentRun.status]"></span>
    <AdjustmentsHorizontalIcon />
    <span>运行详情</span>
  </button>
</template>
