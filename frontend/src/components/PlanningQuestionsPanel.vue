<script setup>
import { ArrowRightIcon, ChevronDownIcon, QuestionMarkCircleIcon } from '@heroicons/vue/24/outline';
import { computed, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const props = defineProps({
  intake: { type: Object, default: null },
  readonly: { type: Boolean, default: false },
});
const store = useWorkspaceStore();
const selections = ref({});
const customAnswers = ref({});
const submitting = ref(false);
const expanded = ref(true);
const intake = computed(() => props.intake || store.planningState.intake);

watch(() => intake.value?.updated_at, () => {
  selections.value = {};
  customAnswers.value = {};
});

watch(() => props.readonly, (readonly) => {
  expanded.value = !readonly;
}, { immediate: true });

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
</script>

<template>
  <section v-if="intake?.open_questions?.length" :class="['planning-panel', 'question-panel', { readonly, collapsed: !expanded }]">
    <header @click="props.readonly && (expanded = !expanded)">
      <span class="planning-icon"><QuestionMarkCircleIcon /></span>
      <div>
        <small>计划共创 · 需求澄清</small>
        <h3>{{ props.readonly ? '这一轮提问' : '先确认几个会改变计划结构的问题' }}</h3>
      </div>
      <span class="readiness">{{ Math.round((intake.readiness_confidence || 0) * 100) }}% 明确</span>
      <ChevronDownIcon v-if="props.readonly" class="panel-chevron" />
    </header>
    <template v-if="expanded">
      <p class="planning-rationale">{{ intake.rationale }}</p>
      <div class="confirmed-facts" v-if="intake.confirmed_facts?.length">
        <span v-for="fact in intake.confirmed_facts.slice(0, 5)" :key="fact.key"><strong>{{ fact.key }}</strong>{{ fact.value }}</span>
      </div>
      <div class="question-list">
        <article v-for="(question, index) in intake.open_questions" :key="question.id" class="planning-question">
          <div><small>问题 {{ index + 1 }}</small><strong>{{ question.prompt }}</strong><p v-if="question.why">{{ question.why }}</p></div>
          <div v-if="!props.readonly && question.options?.length" class="question-options">
            <button
              v-for="option in question.options"
              :key="option"
              :class="{ selected: selections[question.id] === option }"
              @click="selectOption(question, option)"
            >{{ option }}</button>
          </div>
          <div v-else-if="props.readonly && question.options?.length" class="question-options question-options-static">
            <span v-for="option in question.options" :key="option">{{ option }}</span>
          </div>
          <textarea
            v-if="!props.readonly && question.allow_custom"
            v-model="customAnswers[question.id]"
            rows="2"
            :placeholder="question.options?.length ? '也可以补充自己的情况' : '输入你的回答'"
          ></textarea>
        </article>
      </div>
      <footer v-if="!props.readonly"><span>不需要一次填完所有背景；Agent 会自己判断何时信息已经充分。</span><button class="primary" :disabled="submitting" @click="answerQuestions">提交回答 <ArrowRightIcon /></button></footer>
      <footer v-else class="readonly-footer"><span>历史快照 · 该轮提问对应这条消息，后续回答作为独立消息排在下方</span></footer>
    </template>
    <footer v-else class="collapsed-hint"><span>点击展开这一轮提问</span></footer>
  </section>
</template>
