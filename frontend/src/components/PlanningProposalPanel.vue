<script setup>
import {
  ArrowRightIcon,
  CheckCircleIcon,
  ChevronDownIcon,
  ClipboardDocumentCheckIcon,
  UserGroupIcon,
  XMarkIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const props = defineProps({
  proposal: { type: Object, default: null },
  readonly: { type: Boolean, default: false },
});
const store = useWorkspaceStore();
const submitting = ref(false);
const expanded = ref(true);
const proposal = computed(() => props.proposal || store.planningState.proposal);
const pendingProposal = computed(() => proposal.value?.status === 'pending');
const stages = computed(() => proposal.value?.plan_payload?.stages || []);
const taskCount = computed(() => stages.value.reduce((count, stage) => count + (stage.tasks?.length || 0), 0));
const totalMinutes = computed(() => stages.value.reduce(
  (total, stage) => total + (stage.tasks || []).reduce((sum, task) => sum + (task.estimated_minutes || 0), 0),
  0,
));

async function acceptProposal() {
  submitting.value = true;
  await store.decidePlanProposal(proposal.value.id, true);
  submitting.value = false;
}

async function reviseProposal() {
  await store.startRun('我暂时不采用这份计划提案。请保持在当前 Session，先询问我希望调整的关键部分，再更新需求和提案。');
}

async function rejectProposal() {
  submitting.value = true;
  await store.decidePlanProposal(proposal.value.id, false);
  submitting.value = false;
}
</script>

<template>
  <section v-if="proposal && (props.readonly || proposal.status !== 'accepted')" :class="['planning-panel', 'proposal-panel', proposal.status, { readonly: props.readonly, collapsed: !expanded }]">
    <header @click="props.readonly && (expanded = !expanded)">
      <span class="planning-icon"><ClipboardDocumentCheckIcon /></span>
      <div><small>计划提案 · {{ proposal.status === 'pending' ? '等待确认' : proposal.status === 'accepted' ? '已采用' : '已退回' }}</small><h3>{{ proposal.title }}</h3></div>
      <span v-if="proposal.status === 'accepted'" class="readiness accepted">已采用</span>
      <span v-else-if="pendingProposal" class="readiness">尚未创建正式计划</span>
      <XMarkIcon v-else class="proposal-state-icon" />
      <ChevronDownIcon v-if="props.readonly" class="panel-chevron" />
    </header>
    <template v-if="expanded">
      <p class="planning-rationale">{{ proposal.rationale }}</p>
      <div class="proposal-metrics">
        <span><strong>{{ stages.length }}</strong> 阶段</span><span><strong>{{ taskCount }}</strong> 任务</span><span><strong>{{ totalMinutes }}</strong> 预计分钟</span>
      </div>
      <div class="proposal-stages">
        <article v-for="(stage, index) in stages" :key="`${index}-${stage.title}`">
          <span>{{ index + 1 }}</span>
          <div><strong>{{ stage.title }}</strong><p>{{ stage.description || stage.objectives?.join(' · ') }}</p><small>{{ stage.tasks?.length || 0 }} 个任务</small></div>
        </article>
      </div>
      <div v-if="proposal.specialist_reports?.length" class="proposal-specialists"><UserGroupIcon /><span>{{ proposal.specialist_reports.length }} 个规划子 Agent 的结论已被主 Agent 汇总</span></div>
      <footer v-if="!props.readonly && pendingProposal"><button class="danger-quiet" :disabled="submitting" @click="rejectProposal">放弃提案</button><span></span><button @click="reviseProposal">继续讨论</button><button class="primary" :disabled="submitting" @click="acceptProposal">采用并创建计划 <ArrowRightIcon /></button></footer>
      <footer v-else-if="props.readonly" class="readonly-footer"><span>历史快照 · 提案已作为这条消息的一部分归档</span></footer>
    </template>
    <footer v-else class="collapsed-hint"><span>点击展开这份计划提案</span></footer>
  </section>
</template>
