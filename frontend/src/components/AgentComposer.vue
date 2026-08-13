<script setup>
import {
  ArrowUpIcon,
  BellIcon,
  ChevronUpIcon,
  ChevronDownIcon,
  MapIcon,
  PencilSquareIcon,
  PlusIcon,
  SparklesIcon,
  StopIcon,
  TrashIcon,
  XMarkIcon,
} from '@heroicons/vue/24/outline';
import { computed, nextTick, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import api from '../api/client';

const store = useWorkspaceStore();
const prompt = ref('');
const fileInput = ref(null);
const uploading = ref(false);
const queueMenuOpen = ref(false);
const editingQueueId = ref(null);
const editingDraft = ref('');
const queueEditInput = ref(null);
const running = computed(() => ['queued', 'running', 'waiting_approval'].includes(store.currentRun?.status));
const waitingApproval = computed(() => store.currentRun?.status === 'waiting_approval');
const queuedMessages = computed(() => store.queuedMessages.filter(
  (item) => item.session_id === (store.activeSessionId || null),
));
const followUpBehavior = computed(() => store.followUpBehavior);
const defaultActionLabel = computed(() => (
  !waitingApproval.value && followUpBehavior.value === 'steer' ? '转向当前运行' : '排队到下一轮'
));
const contextUsage = computed(() => {
  const event = [...store.runEvents].reverse().find((item) => item.type === 'context.built');
  if (!event?.payload?.estimated_tokens) return null;
  return {
    tokens: event.payload.estimated_tokens,
    window: store.appSettings?.model_context_window || 128000,
  };
});
const contextLabel = computed(() => (
  contextUsage.value ? `${Math.round(contextUsage.value.tokens / 1000)}k / ${Math.round(contextUsage.value.window / 1000)}k` : ''
));
const archivedContext = computed(() => (
  Boolean(store.activeSession?.archived_at) || store.focusedPlan?.status === 'archived'
));
const childContext = computed(() => Boolean(store.currentRun?.parent_run_id));
const composerDisabled = computed(() => archivedContext.value || childContext.value);

async function submit() {
  if (archivedContext.value) return;
  const value = prompt.value.trim();
  if (!value) return;
  if (running.value) {
    await applyFollowUp(value, waitingApproval.value ? 'queue' : followUpBehavior.value);
    return;
  }
  await sendNow(value);
}

async function applyFollowUp(value, mode) {
  queueMenuOpen.value = false;
  if (!value) return;
  if (mode === 'steer' && store.currentRun?.id) {
    const steered = await store.steerRun(store.currentRun.id, value);
    if (steered) prompt.value = '';
    return;
  }
  const queued = await store.enqueueMessage(value);
  if (queued) prompt.value = '';
}

async function tabAction(event) {
  if (!running.value || composerDisabled.value || !prompt.value.trim()) return;
  event.preventDefault();
  const value = prompt.value.trim();
  if (!value) return;
  const queued = await store.enqueueMessage(value);
  if (queued) prompt.value = '';
}

async function sendNow(value, mode = 'normal') {
  const started = await store.startRun(value, undefined, mode === 'interrupt' ? { mode: 'interrupt' } : {});
  if (started) prompt.value = '';
}

async function interruptSend() {
  const value = prompt.value.trim();
  queueMenuOpen.value = false;
  if (!value) return;
  await sendNow(value, 'interrupt');
}

function stopRun() {
  store.cancelCurrentRun();
}

async function beginQueueEdit(message) {
  editingQueueId.value = message.id;
  editingDraft.value = message.user_content || message.objective;
  await nextTick();
  queueEditInput.value?.focus();
  queueEditInput.value?.select();
}

async function saveQueueEdit(message) {
  const value = editingDraft.value.trim();
  if (value && value !== (message.user_content || message.objective)) {
    await store.updateQueuedMessage(message.id, { objective: value });
  }
  editingQueueId.value = null;
}

async function sendQueueNow(message) {
  await store.sendQueuedMessage(message.id);
}

watch(() => store.activeSessionId, () => {
  store.loadQueue();
});

async function uploadFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  uploading.value = true;
  try {
    const response = await api.upload('/workspace/files', file);
    prompt.value = `请读取并检查我上传的学习成果文件 \`${response.data.path}\`。如果当前对话聚焦某个任务，请把它作为 submission_create 的证据；需要时运行代码或测试，再用 submission_check 给出验收结果。`;
  } catch (uploadError) {
    store.error = uploadError.message;
  } finally {
    uploading.value = false;
    event.target.value = '';
  }
}

async function switchSession(event) {
  const sessionId = event.target.value;
  if (!sessionId) {
    store.startNewConversation();
    return;
  }
  const session = store.sessions.find((item) => item.id === sessionId);
  if (session) await store.selectSession(session);
}
</script>

<template>
  <div class="composer-wrap">
    <div v-if="queuedMessages.length" class="queue-stack">
      <div class="queue-stack-head">
        <span><strong>{{ queuedMessages.length }}</strong> 条消息排队中 · 完成或失败后自动发送，主动停止时保留</span>
        <small>Enter={{ defaultActionLabel }} · Tab=排队</small>
      </div>
      <div
        v-for="(message, index) in queuedMessages"
        :key="message.id"
        class="queue-item"
      >
        <span class="queue-index">{{ index + 1 }}</span>
        <template v-if="editingQueueId === message.id">
          <input
            ref="queueEditInput"
            v-model="editingDraft"
            class="queue-edit-input"
            @keydown.enter.exact.prevent="saveQueueEdit(message)"
            @keydown.esc="editingQueueId = null"
          />
        </template>
        <p v-else class="queue-objective">
          <small v-if="message.trigger !== 'user_message'" class="queue-source">
            {{ message.trigger === 'email_reply' ? '邮件回复' : '系统消息' }}
          </small>
          <span>{{ message.user_content || message.objective }}</span>
        </p>
        <div class="queue-actions">
          <button
            title="上移"
            :disabled="index === 0"
            @click="store.moveQueuedMessage(message.id, -1)"
          ><ChevronUpIcon /></button>
          <button
            title="下移"
            :disabled="index === queuedMessages.length - 1"
            @click="store.moveQueuedMessage(message.id, 1)"
          ><ChevronDownIcon /></button>
          <button v-if="message.trigger === 'user_message' && editingQueueId !== message.id" title="编辑" @click="beginQueueEdit(message)"><PencilSquareIcon /></button>
          <button v-else title="保存" @click="saveQueueEdit(message)"><XMarkIcon /></button>
          <button
            v-if="!running"
            title="立即发送"
            @click="sendQueueNow(message)"
          ><ArrowUpIcon /></button>
          <button class="queue-delete" title="删除" @click="store.deleteQueuedMessage(message.id)"><TrashIcon /></button>
        </div>
      </div>
    </div>

    <div class="composer-shell">
      <input ref="fileInput" class="visually-hidden" type="file" @change="uploadFile" />
      <div v-if="store.replyTargetNotification" class="composer-reply-target">
        <BellIcon />
        <span><small>回复提醒</small><strong>{{ store.replyTargetNotification.title }}</strong></span>
        <button title="取消回复提醒" @click="store.replyTargetNotification = null"><XMarkIcon /></button>
      </div>
      <textarea
        v-model="prompt"
        rows="1"
        :placeholder="childContext ? '子 Agent 线程为只读，请返回主对话继续交流' : archivedContext ? '归档内容为只读，恢复后可以继续对话' : uploading ? '正在上传文件…' : waitingApproval ? '请先处理上方确认；补充消息会排到下一轮' : running ? `继续补充要求 · Enter=${defaultActionLabel} · Tab=排队` : '说说你想学什么，或询问现在该做什么'"
        :disabled="composerDisabled"
        @keydown.enter.exact.prevent="submit"
        @keydown.tab.exact="tabAction"
      ></textarea>
      <div class="composer-utility-bar">
        <button class="composer-plus" :disabled="running || uploading || composerDisabled" title="上传学习成果" @click="fileInput.click()"><PlusIcon /></button>
        <button
          v-if="store.focusedPlan"
          class="composer-scope focused"
          title="打开当前学习计划"
          @click="store.selectPlan(store.focusedPlan.id)"
        >
          <MapIcon />
          <span>{{ store.focusedPlan.title }}</span>
        </button>
        <span v-else class="composer-scope composer-scope-static" title="使用学习画像、计划摘要和长期记忆">
          <SparklesIcon />
          <span>综合学习上下文</span>
        </span>
        <small v-if="contextUsage" class="context-usage" :title="`上下文约 ${contextUsage.tokens.toLocaleString()} / ${contextUsage.window.toLocaleString()} tokens`">{{ contextLabel }}</small>
        <span class="composer-mode">Hy3</span>
        <span class="composer-effort">深入思考</span>
        <div class="composer-actions">
        <button
          v-if="running"
          class="send-button running"
          title="停止当前运行"
          @click="stopRun"
        ><StopIcon /></button>
        <button
          v-else
          class="send-button"
          :disabled="composerDisabled || !prompt.trim()"
          title="发送（Shift+Enter 换行）"
          @click="submit"
        ><ArrowUpIcon /></button>
        <button
          v-if="running"
          class="composer-followup"
          title="选择发送方式"
          @click="queueMenuOpen = !queueMenuOpen"
        ><ChevronUpIcon /></button>
        </div>
      </div>
    </div>
    <div v-if="queueMenuOpen" class="queue-menu">
      <button v-if="!waitingApproval" @click="applyFollowUp(prompt.trim(), 'steer')">转向当前运行（不停止）</button>
      <button @click="applyFollowUp(prompt.trim(), 'queue')">排队到下一轮</button>
      <button class="danger" @click="interruptSend">打断当前运行并立即发送</button>
      <button class="quiet" @click="queueMenuOpen = false">取消</button>
    </div>
    <label class="mobile-session-switch composer-mobile-switch">
        <span class="visually-hidden">切换对话</span>
        <select :value="store.activeSessionId || ''" @change="switchSession">
          <option value="">＋ 新对话</option>
          <option v-for="session in store.sessions" :key="session.id" :value="session.id">
            {{ session.title }}
          </option>
        </select>
    </label>
  </div>
</template>
