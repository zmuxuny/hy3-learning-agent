<script setup>
import { BellIcon, ClipboardIcon } from '@heroicons/vue/24/outline';
import { computed, nextTick, ref, watch } from 'vue';
import { usePlanStore } from '../stores/plan.js';
import { useRunStore } from '../stores/run.js';
import { useSessionStore } from '../stores/session.js';
import AgentMessage from './AgentMessage.vue';
import PlanCard from './PlanCard.vue';
import PlanningProposalPanel from './PlanningProposalPanel.vue';
import PlanningQuestionsPanel from './PlanningQuestionsPanel.vue';
import RunDisclosure from './RunDisclosure.vue';
import { isRunBlocking } from '../runState.js';

const props = defineProps({
  answer: { type: String, default: '' },
  userMessage: { type: Object, default: null },
  cards: { type: Array, default: () => [] },
  run: { type: Object, default: null },
  events: { type: Array, default: () => [] },
  historical: { type: Boolean, default: false },
  messageId: { type: [Number, String], default: null },
  messageMetadata: { type: Object, default: () => ({}) },
  highlighted: { type: Boolean, default: false },
});

const emit = defineEmits(['approval-ready']);
const planStore = usePlanStore();
const runStore = useRunStore();
const sessionStore = useSessionStore();
const approvalAnswer = ref('');
const loadingEvents = ref(false);
const displayAnswer = ref('');
let answerFrame = 0;

const resolvedRun = computed(() => props.run || runStore.currentRun);
const live = computed(() => !props.historical && resolvedRun.value?.id === runStore.currentRun?.id);
const resolvedEvents = computed(() => (
  live.value ? runStore.runEvents : props.events
));
const running = computed(() => live.value && isRunBlocking(resolvedRun.value?.status));
const streaming = computed(() => live.value && runStore.streamingRunId === resolvedRun.value?.id && Boolean(runStore.streamingText));
const thinking = computed(() => live.value && runStore.streamingRunId === resolvedRun.value?.id && Boolean(runStore.streamingReasoning) && !runStore.streamingText);
const finalEvent = computed(() => [...resolvedEvents.value].reverse().find((event) => (
  ['assistant.message', 'run.completed', 'run.failed', 'run.cancelled'].includes(event.type || event.event_type)
)));
const answerText = computed(() => (
  streaming.value ? runStore.streamingText : props.answer || finalEvent.value?.summary || ''
));
const approvalPending = computed(() => (
  live.value && resolvedRun.value?.status === 'waiting_approval' && Boolean(resolvedRun.value?.pending_approval)
));
watch(approvalPending, async (pending) => {
  if (pending) {
    await nextTick();
    emit('approval-ready');
  }
}, { immediate: true, flush: 'post' });
const approvalEvent = computed(() => [...resolvedEvents.value].reverse().find((event) => (
  (event.type || event.event_type) === 'approval.required' && event.payload?.blocking
)));
const approvalRequest = computed(() => {
  const call = resolvedRun.value?.pending_approval?.tool_call;
  if (!call) return null;
  try {
    return { name: call.name, args: JSON.parse(call.arguments || '{}') };
  } catch {
    return null;
  }
});
const approvalPlan = computed(() => approvalRequest.value?.name === 'plan_proposal_create' ? approvalRequest.value.args.plan : null);
const approvalSubmission = computed(() => approvalRequest.value?.name === 'submission_create' ? approvalRequest.value.args : null);
const approvalAssessment = computed(() => approvalRequest.value?.name === 'submission_check' ? approvalRequest.value.args : null);
const approvalTitle = computed(() => (
  approvalPlan.value ? '生成可审阅的计划提案'
    : approvalSubmission.value ? '保存学习证据'
      : approvalAssessment.value ? '记录成果验收结果' : '确认待执行操作'
));
const approvalReason = computed(() => {
  const reason = approvalEvent.value?.payload?.reason || resolvedRun.value?.pending_approval?.reason;
  return reason === 'External untrusted content cannot authorize this side effect without approval of the exact request.'
    ? 'Agent 参考了网页或文件。请核对下方具体内容，批准后保存。'
    : reason || '该操作需要你批准后才会执行。';
});
const liveIntakeMatches = computed(() => (
  live.value
  && props.cards.length === 0
  && sessionStore.planningState.intake?.source_run_id === resolvedRun.value?.id
  && (sessionStore.planningState.intake?.open_questions || []).length > 0
));
const liveProposalMatches = computed(() => (
  live.value
  && props.cards.length === 0
  && sessionStore.planningState.proposal?.source_run_id === resolvedRun.value?.id
  && sessionStore.planningState.proposal?.status !== 'accepted'
));
const snapshotCards = computed(() => props.cards.map((card) => {
  if (card.kind === 'planning_questions') {
    const current = Boolean(
      live.value
      && sessionStore.planningState.intake?.source_run_id === card.source_run_id
      && (sessionStore.planningState.intake?.open_questions || []).length > 0
    );
    return { ...card, current };
  }
  if (card.kind === 'plan_proposal') {
    const current = Boolean(
      live.value
      && sessionStore.planningState.proposal?.id === card.proposal?.id
      && sessionStore.planningState.proposal?.status === 'pending'
    );
    return { ...card, current };
  }
  return { ...card, current: false };
}));
const createdPlan = computed(() => {
  const target = runStore.runs.find((item) => item.id === resolvedRun.value?.id);
  if (!target?.created_plan_id) return null;
  return planStore.allPlans.find((plan) => Number(plan.id) === Number(target.created_plan_id))
    || { id: Number(target.created_plan_id), title: `计划 ${target.created_plan_id}` };
});
const proactive = computed(() => props.messageMetadata?.ui_kind === 'proactive_notification');

watch(answerText, (text) => {
  cancelAnimationFrame(answerFrame);
  answerFrame = requestAnimationFrame(() => {
    displayAnswer.value = text;
  });
}, { immediate: true });

async function ensureEvents() {
  if (!props.historical || props.events.length || !resolvedRun.value?.id) return;
  loadingEvents.value = true;
  try {
    await runStore.loadRunEvents(resolvedRun.value.id);
  } finally {
    loadingEvents.value = false;
  }
}

async function copyAnswer() {
  const text = answerText.value.trim();
  if (text) await navigator.clipboard?.writeText(text);
}
</script>

<template>
  <article
    :id="props.messageId ? `message-${props.messageId}` : undefined"
    :class="['thread-run', { live: running, historical: props.historical, proactive, highlighted: props.highlighted }]"
  >
    <div class="thread-run-content">
      <RunDisclosure
        v-if="resolvedRun"
        :run="resolvedRun"
        :events="resolvedEvents"
        :loading="loadingEvents"
        @expand="ensureEvents"
      />

      <header v-if="proactive" class="proactive-message-heading">
        <BellIcon />
        <div>
          <small>Agent 主动提醒</small>
          <strong>{{ props.messageMetadata.notification_title || '学习进度跟进' }}</strong>
        </div>
      </header>

      <template v-for="card in snapshotCards" :key="`${card.kind}-${card.created_at}`">
        <PlanningQuestionsPanel
          v-if="card.kind === 'planning_questions'"
          :intake="card.current ? null : card.intake"
          :readonly="!card.current"
        />
        <PlanningProposalPanel
          v-else-if="card.kind === 'plan_proposal'"
          :proposal="card.current ? null : card.proposal"
          :readonly="!card.current"
        />
      </template>
      <template v-if="props.cards.length === 0">
        <PlanningQuestionsPanel v-if="liveIntakeMatches" />
        <PlanningProposalPanel v-if="liveProposalMatches" />
      </template>

      <section v-if="approvalPending" class="approval-card thread-approval-card">
        <div class="approval-copy">
          <small>需要你的确认</small>
          <strong>{{ approvalTitle }}</strong>
          <p>{{ approvalReason }}</p>
        </div>
        <details v-if="approvalRequest" class="approval-request-preview" open>
          <summary>{{ approvalPlan ? approvalPlan.title : approvalSubmission ? `任务 ${approvalSubmission.task_id} 的提交内容` : approvalAssessment ? `提交 ${approvalAssessment.submission_id} 的验收结果` : '查看待执行内容' }}</summary>
          <template v-if="approvalPlan">
            <p>每周 {{ approvalPlan.weekly_minutes }} 分钟 · {{ approvalPlan.description }}</p>
            <p>{{ approvalPlan.expected_outcome }}</p>
            <div v-for="(stage, index) in approvalPlan.stages" :key="index" class="approval-stage">
              <strong>{{ stage.title }}</strong>
              <details v-for="(task, taskIndex) in stage.tasks" :key="taskIndex" class="approval-task">
                <summary>{{ task.title }} · {{ task.estimated_minutes }} 分钟</summary>
                <p>{{ task.description }}</p>
              </details>
            </div>
            <p>批准后生成提案；采用提案时再创建学习计划。</p>
          </template>
          <template v-else-if="approvalSubmission">
            <p v-for="(artifact, index) in approvalSubmission.artifacts" :key="index">{{ artifact.path || artifact.url }}<br>{{ artifact.note }}</p>
            <pre>{{ approvalSubmission.content }}</pre>
          </template>
          <template v-else-if="approvalAssessment">
            <p>评分 {{ approvalAssessment.score }} · 通过线 {{ approvalAssessment.pass_threshold ?? 70 }}</p>
            <div v-for="(check, index) in approvalAssessment.checks" :key="index">
              <strong>{{ check.name || check.criterion }} · {{ check.result === 'pass' ? '通过' : check.result === 'fail' ? '未通过' : check.result }}</strong>
              <p>{{ check.evidence }}</p>
            </div>
            <pre>{{ approvalAssessment.feedback }}</pre>
          </template>
          <pre v-else>{{ JSON.stringify(approvalRequest.args, null, 2) }}</pre>
        </details>
        <textarea
          v-model="approvalAnswer"
          class="approval-answer"
          rows="2"
          placeholder="也可以补充要求，Agent 会据此调整…"
        ></textarea>
        <div class="approval-actions">
          <button class="secondary-button" @click="runStore.decideRunApproval(resolvedRun.id, false)">拒绝</button>
          <button
            v-if="approvalAnswer.trim()"
            class="secondary-button"
            @click="runStore.decideRunApproval(resolvedRun.id, false, approvalAnswer.trim())"
          >回答并继续</button>
          <button class="primary-button" @click="runStore.decideRunApproval(resolvedRun.id, true)">批准</button>
        </div>
      </section>

      <div v-if="thinking" class="thinking-row inline-thinking">
        <span></span><span></span><span></span><p>{{ runStore.streamingReasoning || '正在理解上下文并决定下一步' }}</p>
      </div>

      <div v-if="displayAnswer" :class="['assistant-answer', { failed: finalEvent?.type === 'run.failed', streaming }]">
        <AgentMessage :content="displayAnswer" />
        <span v-if="streaming" class="stream-cursor" aria-hidden="true"></span>
        <small v-if="finalEvent?.type === 'run.failed'">错误编号：{{ finalEvent.payload?.code || 'run_failed' }}</small>
        <div class="assistant-message-actions">
          <button title="复制回答" @click="copyAnswer"><ClipboardIcon /></button>
        </div>
      </div>

      <PlanCard v-if="createdPlan" :plan="createdPlan" />
    </div>
  </article>
</template>

<style scoped>
.approval-request-preview { min-width: 0; line-height: 1.65; overflow-wrap: anywhere; }
.approval-stage { margin-top: 14px; }
.approval-task { margin-top: 4px; }
.approval-task summary { padding: 6px 0; font-weight: 400; }
.approval-task p { white-space: pre-wrap; }
.thread-approval-card { scroll-margin-block: 20px; }
.approval-request-preview summary { cursor: pointer; font-weight: 600; }
.approval-request-preview p { margin: 10px 0; }
.approval-request-preview pre { white-space: pre-wrap; }
</style>
