<script setup>
import { BellIcon, CheckIcon, ChevronDownIcon, ClipboardIcon, PencilSquareIcon, XMarkIcon } from '@heroicons/vue/24/outline';
import { computed, nextTick, ref } from 'vue';
import { useInboxStore } from '../stores/inbox.js';
import { useRunStore } from '../stores/run.js';
import { useShellStore } from '../stores/shell.js';
import { isRunBlocking } from '../runState.js';

const props = defineProps({ message: { type: Object, required: true } });
const inbox = useInboxStore();
const runStore = useRunStore();
const shell = useShellStore();
const editing = ref(false);
const draft = ref('');
const saving = ref(false);
const answersExpanded = ref(false);
const textarea = ref(null);
const planningAnswers = computed(() => (
  props.message.message_metadata?.ui_kind === 'planning_answers'
    ? (props.message.message_metadata.answers || [])
    : null
));
const canEdit = computed(() => !isRunBlocking(runStore.currentRun?.status));
const repliedNotification = computed(() => {
  const interventionId = props.message.reply_to_intervention_id;
  if (!interventionId) return null;
  return inbox.allNotifications.find((item) => item.intervention_id === interventionId)
    || { intervention_id: interventionId, title: `提醒 ${interventionId}` };
});

async function beginEdit() {
  if (!canEdit.value) return;
  draft.value = props.message.content;
  editing.value = true;
  await nextTick();
  textarea.value?.focus();
  textarea.value?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function save() {
  if (!draft.value.trim() || draft.value.trim() === props.message.content) {
    editing.value = false;
    return;
  }
  saving.value = true;
  const succeeded = await shell.editMessage(props.message.id, draft.value.trim());
  saving.value = false;
  if (succeeded) editing.value = false;
}

async function copyMessage() {
  await navigator.clipboard?.writeText(props.message.content);
}
</script>

<template>
  <div :class="['user-message-wrap', { editing }]">
    <small v-if="repliedNotification && !editing" class="user-reply-context">
      <BellIcon />回复提醒 · {{ repliedNotification.title }}
    </small>
    <section v-if="planningAnswers && !editing" class="planning-answers-card">
      <button class="planning-answers-summary" @click="answersExpanded = !answersExpanded">
        <CheckIcon />
        <span><strong>计划澄清已提交 · {{ planningAnswers.length }} 个回答</strong><small>{{ answersExpanded ? '收起' : '点击展开查看回答' }}</small></span>
        <ChevronDownIcon class="activity-chevron" />
      </button>
      <div v-if="answersExpanded" class="planning-answers-body">
        <div v-for="answer in planningAnswers" :key="answer.question_id" class="planning-answer-row">
          <small>{{ answer.question_id }}</small>
          <p>{{ answer.answer }}</p>
        </div>
      </div>
    </section>
    <div v-else-if="!editing" class="message-bubble">{{ message.content }}</div>
    <div v-else class="message-editor">
      <textarea ref="textarea" v-model="draft" rows="3" @keydown.meta.enter="save" @keydown.ctrl.enter="save"></textarea>
      <p>保存后会在当前 Session 重新运行；原版本和原 Run 会保留，已执行的工具操作不会自动撤销。</p>
      <div>
        <button @click="editing = false"><XMarkIcon /> 取消</button>
        <button class="primary" :disabled="saving" @click="save"><CheckIcon /> {{ saving ? '保存中' : '保存并重新运行' }}</button>
      </div>
    </div>
    <div v-if="!editing && !message.pending && !planningAnswers" class="message-actions">
      <button title="复制消息" @click="copyMessage"><ClipboardIcon /></button>
      <button :disabled="!canEdit" :title="canEdit ? '编辑并重新运行' : '请先停止当前运行'" @click="beginEdit"><PencilSquareIcon /></button>
    </div>
  </div>
</template>
