<script setup>
import {
  ArrowRightIcon,
  ArrowsPointingOutIcon,
  CheckCircleIcon,
  ChevronDownIcon,
  ClipboardDocumentCheckIcon,
  ClipboardIcon,
  UserGroupIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useSessionStore } from '../stores/session.js';
import { useShellStore } from '../stores/shell.js';

const props = defineProps({
  proposal: { type: Object, default: null },
  readonly: { type: Boolean, default: false },
});
const sessionStore = useSessionStore();
const shell = useShellStore();
const submitting = ref(false);
const expanded = ref(true);
const copied = ref(false);
const proposal = computed(() => props.proposal || sessionStore.planningState.proposal);
const pendingProposal = computed(() => proposal.value?.status === 'pending');
const stages = computed(() => proposal.value?.plan_payload?.stages || []);
const taskCount = computed(() => stages.value.reduce((count, stage) => count + (stage.tasks?.length || 0), 0));
const totalMinutes = computed(() => stages.value.reduce(
  (total, stage) => total + (stage.tasks || []).reduce((sum, task) => sum + (task.estimated_minutes || 0), 0),
  0,
));

async function acceptProposal() {
  submitting.value = true;
  await shell.decidePlanProposal(proposal.value.id, true);
  submitting.value = false;
}

async function reviseProposal() {
  await shell.startRun('我暂时不采用这份计划提案。请保持在当前 Session，先询问我希望调整的关键部分，再更新需求和提案。');
}

async function rejectProposal() {
  submitting.value = true;
  await shell.decidePlanProposal(proposal.value.id, false);
  submitting.value = false;
}

async function copyProposal() {
  const text = [
    `# ${proposal.value.title}`,
    '',
    proposal.value.rationale || '',
    '',
    ...stages.value.flatMap((stage, index) => [
      `## ${index + 1}. ${stage.title}`,
      stage.description || stage.objectives?.join(' · ') || '',
    ]),
  ].join('\n');
  await navigator.clipboard?.writeText(text);
  copied.value = true;
  window.setTimeout(() => { copied.value = false; }, 1400);
}
</script>

<template>
  <section v-if="proposal && (props.readonly || proposal.status !== 'accepted')" :class="['artifact-preview', 'proposal-artifact', proposal.status, { readonly: props.readonly, expanded }]">
    <header class="artifact-toolbar">
      <span class="artifact-kind"><ClipboardDocumentCheckIcon /> 计划提案</span>
      <span class="artifact-status">{{ proposal.status === 'pending' ? '等待确认' : proposal.status === 'accepted' ? '已采用' : '已退回' }}</span>
      <div class="artifact-actions">
        <button :title="copied ? '已复制' : '复制提案'" @click="copyProposal"><CheckCircleIcon v-if="copied" /><ClipboardIcon v-else /></button>
        <button title="展开或收起" @click="expanded = !expanded"><ArrowsPointingOutIcon /></button>
      </div>
    </header>
    <div class="artifact-viewport">
      <div class="artifact-document">
        <h2>{{ proposal.title }}</h2>
        <p class="artifact-lead">{{ proposal.rationale }}</p>
      <div class="proposal-metrics">
        <span><strong>{{ stages.length }}</strong> 阶段</span><span><strong>{{ taskCount }}</strong> 任务</span><span><strong>{{ totalMinutes }}</strong> 预计分钟</span>
      </div>
      <div class="proposal-stages">
        <article v-for="(stage, index) in stages" :key="`${index}-${stage.title}`">
          <span>{{ index + 1 }}</span>
          <div>
            <strong>{{ stage.title }}</strong><p>{{ stage.description || stage.objectives?.join(' · ') }}</p>
            <details v-for="(task, taskIndex) in stage.tasks" :key="taskIndex" class="proposal-task">
              <summary>{{ task.title }} · {{ task.estimated_minutes }} 分钟</summary>
              <p>{{ task.description }}</p>
              <ul v-if="task.metadata?.acceptance?.length"><li v-for="criterion in task.metadata.acceptance" :key="criterion">{{ criterion }}</li></ul>
            </details>
          </div>
        </article>
      </div>
      <div v-if="proposal.specialist_reports?.length" class="proposal-specialists"><UserGroupIcon /><span>{{ proposal.specialist_reports.length }} 个规划子 Agent 的结论已被主 Agent 汇总</span></div>
      </div>
    </div>
    <button class="artifact-expand" :title="expanded ? '收起预览' : '展开预览'" @click="expanded = !expanded"><ChevronDownIcon /></button>
    <footer v-if="!props.readonly && pendingProposal" class="artifact-footer proposal-actions"><button class="danger-quiet" :disabled="submitting" @click="rejectProposal">放弃提案</button><span></span><button @click="reviseProposal">继续讨论</button><button class="artifact-primary-action" :disabled="submitting" @click="acceptProposal">采用并创建计划 <ArrowRightIcon /></button></footer>
    <footer v-else class="artifact-footer artifact-history-note"><span>{{ proposal.status === 'accepted' ? '这份提案已采用' : '历史提案快照' }}</span></footer>
  </section>
</template>

<style scoped>
.proposal-task { margin-top: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.proposal-task summary { cursor: pointer; padding: 6px 0; }
.proposal-task li { margin: 4px 0; }
</style>
