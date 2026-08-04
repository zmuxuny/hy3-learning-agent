<script setup>
import {
  ArrowTopRightOnSquareIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  UserGroupIcon,
  XCircleIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import api from '../api/client';

const store = useWorkspaceStore();
const expanded = ref(true);
const agents = computed(() => store.activeSubagents);

async function stopAll() {
  for (const agent of agents.value) {
    await api.post(`/agent/runs/${agent.child_run_id}/cancel`);
  }
}

async function stopOne(agent) {
  await api.post(`/agent/runs/${agent.child_run_id}/cancel`);
}

function openThread(agent) {
  store.inspectRun({
    id: agent.child_run_id,
    plan_id: store.currentRun?.plan_id ?? null,
    session_id: store.currentRun?.session_id ?? null,
    status: 'running',
    objective: agent.objective,
  });
}
</script>

<template>
  <section v-if="agents.length" class="subagent-panel">
    <button class="subagent-panel-summary" @click="expanded = !expanded">
      <span class="subagent-panel-icon"><UserGroupIcon /></span>
      <span class="subagent-panel-copy">
        <strong>{{ agents.length }} 个子 Agent 正在并行工作</strong>
        <small>{{ expanded ? '收起面板' : '点击展开查看状态' }}</small>
      </span>
      <span class="subagent-panel-actions">
        <button title="停止全部子 Agent" @click.stop="stopAll"><XCircleIcon /> 全部停止</button>
      </span>
      <ChevronUpIcon v-if="expanded" class="subagent-panel-chevron" />
      <ChevronDownIcon v-else class="subagent-panel-chevron" />
    </button>
    <div v-if="expanded" class="subagent-panel-body">
      <div v-for="agent in agents" :key="agent.child_run_id" class="subagent-live-row">
        <span class="live-dot"></span>
        <div>
          <strong>{{ agent.role }}</strong>
          <p>{{ agent.objective }}</p>
        </div>
        <div class="subagent-live-actions">
          <button title="打开线程" @click="openThread(agent)"><ArrowTopRightOnSquareIcon /></button>
          <button title="停止" @click="stopOne(agent)"><XCircleIcon /></button>
        </div>
      </div>
    </div>
  </section>
</template>
