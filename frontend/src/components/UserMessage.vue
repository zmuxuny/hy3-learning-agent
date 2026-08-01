<script setup>
import { CheckIcon, ClipboardIcon, PencilSquareIcon, XMarkIcon } from '@heroicons/vue/24/outline';
import { computed, nextTick, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const props = defineProps({ message: { type: Object, required: true } });
const store = useWorkspaceStore();
const editing = ref(false);
const draft = ref('');
const saving = ref(false);
const textarea = ref(null);
const canEdit = computed(() => !['queued', 'running', 'waiting_approval'].includes(store.currentRun?.status));

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
  const succeeded = await store.editMessage(props.message.id, draft.value.trim());
  saving.value = false;
  if (succeeded) editing.value = false;
}

async function copyMessage() {
  await navigator.clipboard?.writeText(props.message.content);
}
</script>

<template>
  <div :class="['user-message-wrap', { editing }]">
    <div v-if="!editing" class="message-bubble">{{ message.content }}</div>
    <div v-else class="message-editor">
      <textarea ref="textarea" v-model="draft" rows="3" @keydown.meta.enter="save" @keydown.ctrl.enter="save"></textarea>
      <p>保存后会在当前 Session 重新运行；原版本和原 Run 会保留，已执行的工具操作不会自动撤销。</p>
      <div>
        <button @click="editing = false"><XMarkIcon /> 取消</button>
        <button class="primary" :disabled="saving" @click="save"><CheckIcon /> {{ saving ? '保存中' : '保存并重新运行' }}</button>
      </div>
    </div>
    <div v-if="!editing && !message.pending" class="message-actions">
      <button title="复制消息" @click="copyMessage"><ClipboardIcon /></button>
      <button :disabled="!canEdit" :title="canEdit ? '编辑并重新运行' : '请先停止当前运行'" @click="beginEdit"><PencilSquareIcon /></button>
    </div>
  </div>
</template>
