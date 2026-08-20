<script setup>
import { AdjustmentsHorizontalIcon } from '@heroicons/vue/24/outline';
import { useRunStore } from '../stores/run.js';
import { useShellStore } from '../stores/shell.js';

const runStore = useRunStore();
const shell = useShellStore();
</script>

<template>
  <button class="trace-toggle" @click="shell.traceOpen = true">
    <span v-if="runStore.currentRun" :class="['status-dot', runStore.currentRun.status]"></span>
    <AdjustmentsHorizontalIcon />
    <span>运行详情</span>
  </button>
</template>
