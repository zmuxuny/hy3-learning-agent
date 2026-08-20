<script setup>
import { SparklesIcon } from '@heroicons/vue/24/outline';
import { computed, nextTick, ref, watch } from 'vue';
import { usePlanStore } from '../stores/plan.js';
import { useRunStore } from '../stores/run.js';
import { useSessionStore } from '../stores/session.js';
import { useShellStore } from '../stores/shell.js';
import AgentComposer from './AgentComposer.vue';
import AgentRunTurn from './AgentRunTurn.vue';
import UserMessage from './UserMessage.vue';

const planStore = usePlanStore();
const runStore = useRunStore();
const sessionStore = useSessionStore();
const shell = useShellStore();
const scrollArea = ref(null);
const pinnedToBottom = ref(true);
const currentRunUser = computed(() => [...sessionStore.conversationMessages].reverse().find(
  (message) => message.run_id === runStore.currentRun?.id && message.role === 'user',
));
const currentRunAssistant = computed(() => [...sessionStore.conversationMessages].reverse().find(
  (message) => message.run_id === runStore.currentRun?.id && message.role === 'assistant',
));
const suggestions = [
  '根据我的目标创建一份完整学习计划',
  '检查我现在的计划，告诉我今天最该做什么',
  '根据最近表现主动抽查我',
];

function onScroll() {
  if (!scrollArea.value) return;
  const distance = scrollArea.value.scrollHeight - scrollArea.value.scrollTop - scrollArea.value.clientHeight;
  pinnedToBottom.value = distance < 120;
}

async function scrollToLatest(force = false) {
  await nextTick();
  if (scrollArea.value && (force || pinnedToBottom.value)) {
    scrollArea.value.scrollTop = scrollArea.value.scrollHeight;
  }
}

watch(() => runStore.currentRun?.id, () => {
  pinnedToBottom.value = true;
  scrollToLatest(true);
});
watch(() => sessionStore.conversationMessages.length, () => scrollToLatest());
watch(() => runStore.runEvents.length, () => scrollToLatest());
watch(() => shell.highlightedMessageId, async (messageId) => {
  if (!messageId) return;
  await nextTick();
  document.getElementById(`message-${messageId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  window.setTimeout(() => {
    if (shell.highlightedMessageId === messageId) shell.highlightedMessageId = null;
  }, 2400);
});
</script>

<template>
  <section class="conversation-page">
    <div ref="scrollArea" class="conversation-scroll" @scroll="onScroll">
      <div v-if="!runStore.currentRun && !sessionStore.conversationMessages.length" class="welcome-state">
        <div class="welcome-mark"><SparklesIcon /></div>
        <h1>{{ sessionStore.activeSession?.handoff_summary ? `继续推进${planStore.focusedPlan ? `「${planStore.focusedPlan.title}」` : '计划'}` : '今天想学什么？' }}</h1>
        <p v-if="sessionStore.activeSession?.handoff_summary">已从原对话带入目标、决定和未解决事项；这里开始只使用该计划的任务、证据与记忆。</p>
        <p v-else>告诉我你的目标。我会制定计划、持续跟进，并在需要时主动提醒或考核。</p>
        <div v-if="!sessionStore.activeSession?.handoff_summary" class="suggestion-list">
          <button v-for="suggestion in suggestions" :key="suggestion" @click="shell.startRun(suggestion)">
            {{ suggestion }}
          </button>
        </div>

      </div>

      <div v-else class="thread">
        <template v-for="message in sessionStore.conversationMessages" :key="message.id">
          <div v-if="message.role === 'user'" class="user-turn">
            <UserMessage :message="message" />
          </div>

          <AgentRunTurn
            v-if="message.role === 'user' && message.id === currentRunUser?.id"
            :answer="currentRunAssistant?.content || ''"
            :user-message="currentRunUser"
            :cards="currentRunAssistant?.message_metadata?.cards || []"
            :message-id="currentRunAssistant?.id"
            :message-metadata="currentRunAssistant?.message_metadata || {}"
            :highlighted="currentRunAssistant?.id === shell.highlightedMessageId"
          />

          <AgentRunTurn
            v-if="message.role === 'assistant' && message.run_id !== runStore.currentRun?.id"
            :answer="message.content"
            :run="runStore.runForId(message.run_id)"
            :events="runStore.eventsForRun(message.run_id)"
            :cards="message.message_metadata?.cards || []"
            :message-id="message.id"
            :message-metadata="message.message_metadata || {}"
            :highlighted="message.id === shell.highlightedMessageId"
            historical
          />
        </template>

        <template v-if="runStore.currentRun && !currentRunUser">
          <div v-if="currentRunAssistant?.message_metadata?.ui_kind !== 'proactive_notification'" class="user-turn run-objective-turn">
            <div class="message-bubble">{{ runStore.currentRun.objective }}</div>
          </div>
          <AgentRunTurn
            :answer="currentRunAssistant?.content || ''"
            :user-message="currentRunUser"
            :message-id="currentRunAssistant?.id"
            :message-metadata="currentRunAssistant?.message_metadata || {}"
            :highlighted="currentRunAssistant?.id === shell.highlightedMessageId"
          />
        </template>

      </div>
    </div>

    <AgentComposer />
  </section>
</template>
