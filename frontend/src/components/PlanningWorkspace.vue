<script setup>
import {
  ArrowRightIcon,
  CheckCircleIcon,
  ClipboardDocumentCheckIcon,
  QuestionMarkCircleIcon,
  UserGroupIcon,
  XMarkIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const selections = ref({});
const customAnswers = ref({});
const submitting = ref(false);
const intake = computed(() => store.planningState.intake);
const proposal = computed(() => store.planningState.proposal);
const pendingProposal = computed(() => proposal.value?.status === 'pending');
const stages = computed(() => proposal.value?.plan_payload?.stages || []);
const taskCount = computed(() => stages.value.reduce((count, stage) => count + (stage.tasks?.length || 0), 0));
const totalMinutes = computed(() => stages.value.reduce(
  (total, stage) => total + (stage.tasks || []).reduce((sum, task) => sum + (task.estimated_minutes || 0), 0),
  0,
));

watch(() => intake.value?.updated_at, () => {
  selections.value = {};
  customAnswers.value = {};
});

function selectOption(question, option) {
  selections.value = { ...selections.value, [question.id]: option };
}

async function answerQuestions() {
  const answers = (intake.value?.open_questions || []).map((question) => ({
    question_id: question.id,
    answer: customAnswers.value[question.id]?.trim() || selections.value[question.id] || '交给 AI 判断',
  }));
  submitting.value = true;
  await store.submitPlanningAnswers(answers);
  submitting.value = false;
}

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
  <section v-if="intake?.open_questions?.length" class="planning-panel question-panel">
    <header>
      <span class="planning-icon"><QuestionMarkCircleIcon /></span>
      <div><small>计划共创 · 需求澄清</small><h3>先确认几个会改变计划结构的问题</h3></div>
      <span class="readiness">{{ Math.round((intake.readiness_confidence || 0) * 100) }}% 明确</span>
    </header>
    <p class="planning-rationale">{{ intake.rationale }}</p>
    <div class="confirmed-facts" v-if="intake.confirmed_facts?.length">
      <span v-for="fact in intake.confirmed_facts.slice(0, 5)" :key="fact.key"><strong>{{ fact.key }}</strong>{{ fact.value }}</span>
    </div>
    <div class="question-list">
      <article v-for="(question, index) in intake.open_questions" :key="question.id" class="planning-question">
        <div><small>问题 {{ index + 1 }}</small><strong>{{ question.prompt }}</strong><p v-if="question.why">{{ question.why }}</p></div>
        <div v-if="question.options?.length" class="question-options">
          <button
            v-for="option in question.options"
            :key="option"
            :class="{ selected: selections[question.id] === option }"
            @click="selectOption(question, option)"
          >{{ option }}</button>
        </div>
        <textarea
          v-if="question.allow_custom"
          v-model="customAnswers[question.id]"
          rows="2"
          :placeholder="question.options?.length ? '也可以补充自己的情况' : '输入你的回答'"
        ></textarea>
      </article>
    </div>
    <footer><span>不需要一次填完所有背景；Agent 会自己判断何时信息已经充分。</span><button class="primary" :disabled="submitting" @click="answerQuestions">提交回答 <ArrowRightIcon /></button></footer>
  </section>

  <section v-if="proposal && proposal.status !== 'accepted'" :class="['planning-panel', 'proposal-panel', proposal.status]">
    <header>
      <span class="planning-icon"><ClipboardDocumentCheckIcon /></span>
      <div><small>计划提案 · {{ proposal.status === 'pending' ? '等待确认' : proposal.status === 'accepted' ? '已采用' : '已退回' }}</small><h3>{{ proposal.title }}</h3></div>
      <span v-if="pendingProposal" class="readiness">尚未创建正式计划</span>
      <CheckCircleIcon v-else-if="proposal.status === 'accepted'" class="proposal-state-icon" />
      <XMarkIcon v-else class="proposal-state-icon" />
    </header>
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
    <footer v-if="pendingProposal"><button class="danger-quiet" :disabled="submitting" @click="rejectProposal">放弃提案</button><span></span><button @click="reviseProposal">继续讨论</button><button class="primary" :disabled="submitting" @click="acceptProposal">采用并创建计划 <ArrowRightIcon /></button></footer>
  </section>
</template>
