<script setup>
import { BellIcon, ClipboardIcon } from '@heroicons/vue/24/outline';
import { computed, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentMessage from './AgentMessage.vue';
import PlanCard from './PlanCard.vue';
import PlanningProposalPanel from './PlanningProposalPanel.vue';
import PlanningQuestionsPanel from './PlanningQuestionsPanel.vue';
import RunDisclosure from './RunDisclosure.vue';

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

const store = useWorkspaceStore();
const approvalAnswer = ref('');
const loadingEvents = ref(false);
const displayAnswer = ref('');
let answerFrame = 0;

const resolvedRun = computed(() => props.run || store.currentRun);
const live = computed(() => !props.historical && resolvedRun.value?.id === store.currentRun?.id);
const resolvedEvents = computed(() => (
  live.value ? store.runEvents : props.events
));
const running = computed(() => live.value && ['queued', 'running', 'waiting_approval'].includes(resolvedRun.value?.status));
const streaming = computed(() => live.value && store.streamingRunId === resolvedRun.value?.id && Boolean(store.streamingText));
const thinking = computed(() => live.value && store.streamingRunId === resolvedRun.value?.id && Boolean(store.streamingReasoning) && !store.streamingText);
const finalEvent = computed(() => [...resolvedEvents.value].reverse().find((event) => (
  ['assistant.message', 'run.completed', 'run.failed', 'run.cancelled'].includes(event.type || event.event_type)
)));
const answerText = computed(() => (
  streaming.value ? store.streamingText : props.answer || finalEvent.value?.summary || ''
));
const approvalPending = computed(() => (
  live.value && resolvedRun.value?.status === 'waiting_approval' && Boolean(resolvedRun.value?.pending_approval)
));
const approvalEvent = computed(() => [...resolvedEvents.value].reverse().find((event) => (
  (event.type || event.event_type) === 'approval.required' && event.payload?.blocking
)));
const liveIntakeMatches = computed(() => (
  live.value
  && props.cards.length === 0
  && store.planningState.intake?.source_run_id === resolvedRun.value?.id
  && (store.planningState.intake?.open_questions || []).length > 0
));
const liveProposalMatches = computed(() => (
  live.value
  && props.cards.length === 0
  && store.planningState.proposal?.source_run_id === resolvedRun.value?.id
  && store.planningState.proposal?.status !== 'accepted'
));
const snapshotCards = computed(() => props.cards.map((card) => {
  if (card.kind === 'planning_questions') {
    const current = Boolean(
      live.value
      && store.planningState.intake?.source_run_id === card.source_run_id
      && (store.planningState.intake?.open_questions || []).length > 0
    );
    return { ...card, current };
  }
  if (card.kind === 'plan_proposal') {
    const current = Boolean(
      live.value
      && store.planningState.proposal?.id === card.proposal?.id
      && store.planningState.proposal?.status === 'pending'
    );
    return { ...card, current };
  }
  return { ...card, current: false };
}));
const createdPlan = computed(() => store.planForRun(resolvedRun.value?.id));
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
    await store.loadRunEvents(resolvedRun.value.id);
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
    <span class="thread-run-rail" aria-hidden="true"></span>
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
          <strong>{{ approvalEvent?.payload?.tool_name || 'Agent 操作' }}</strong>
          <p>{{ approvalEvent?.payload?.reason || resolvedRun.pending_approval?.reason || '该操作需要你批准后才会执行。' }}</p>
        </div>
        <textarea
          v-model="approvalAnswer"
          class="approval-answer"
          rows="2"
          placeholder="也可以补充要求，Agent 会据此调整…"
        ></textarea>
        <div class="approval-actions">
          <button class="secondary-button" @click="store.decideRunApproval(resolvedRun.id, false)">拒绝</button>
          <button
            v-if="approvalAnswer.trim()"
            class="secondary-button"
            @click="store.decideRunApproval(resolvedRun.id, false, approvalAnswer.trim())"
          >回答并继续</button>
          <button class="primary-button" @click="store.decideRunApproval(resolvedRun.id, true)">批准</button>
        </div>
      </section>

      <div v-if="thinking" class="thinking-row inline-thinking">
        <span></span><span></span><span></span><p>{{ store.streamingReasoning || '正在理解上下文并决定下一步' }}</p>
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
