<script setup>
import { SparklesIcon } from '@heroicons/vue/24/outline';
import { computed, nextTick, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentComposer from './AgentComposer.vue';
import AgentRunTurn from './AgentRunTurn.vue';
import UserMessage from './UserMessage.vue';

const store = useWorkspaceStore();
const scrollArea = ref(null);
const pinnedToBottom = ref(true);
const currentRunUser = computed(() => store.conversationMessages.find(
  (message) => message.run_id === store.currentRun?.id && message.role === 'user',
));
const currentRunAssistant = computed(() => [...store.conversationMessages].reverse().find(
  (message) => message.run_id === store.currentRun?.id && message.role === 'assistant',
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

watch(() => store.currentRun?.id, () => {
  pinnedToBottom.value = true;
  scrollToLatest(true);
});
watch(() => store.conversationMessages.length, () => scrollToLatest());
watch(() => store.runEvents.length, () => scrollToLatest());
watch(() => store.highlightedMessageId, async (messageId) => {
  if (!messageId) return;
  await nextTick();
  document.getElementById(`message-${messageId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  window.setTimeout(() => {
    if (store.highlightedMessageId === messageId) store.highlightedMessageId = null;
  }, 2400);
});
</script>

<template>
  <section class="conversation-page">
    <div ref="scrollArea" class="conversation-scroll" @scroll="onScroll">
      <div v-if="store.error" class="error-banner">{{ store.error }}</div>

      <div v-if="!store.currentRun && !store.conversationMessages.length" class="welcome-state">
        <div class="welcome-mark"><SparklesIcon /></div>
        <h1>{{ store.activeSession?.handoff_summary ? `继续推进${store.focusedPlan ? `「${store.focusedPlan.title}」` : '计划'}` : '今天想学什么？' }}</h1>
        <p v-if="store.activeSession?.handoff_summary">已从原对话带入目标、决定和未解决事项；这里开始只使用该计划的任务、证据与记忆。</p>
        <p v-else>告诉我你的目标。我会制定计划、持续跟进，并在需要时主动提醒或考核。</p>
        <div v-if="!store.activeSession?.handoff_summary" class="suggestion-list">
          <button v-for="suggestion in suggestions" :key="suggestion" @click="store.startRun(suggestion)">
            {{ suggestion }}
          </button>
        </div>

      </div>

      <div v-else class="thread">
        <template v-for="message in store.conversationMessages" :key="message.id">
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
            :highlighted="currentRunAssistant?.id === store.highlightedMessageId"
          />

          <AgentRunTurn
            v-if="message.role === 'assistant' && message.run_id !== store.currentRun?.id"
            :answer="message.content"
            :run="store.runForId(message.run_id)"
            :events="store.eventsForRun(message.run_id)"
            :cards="message.message_metadata?.cards || []"
            :message-id="message.id"
            :message-metadata="message.message_metadata || {}"
            :highlighted="message.id === store.highlightedMessageId"
            historical
          />
        </template>

        <template v-if="store.currentRun && !currentRunUser">
          <div v-if="currentRunAssistant?.message_metadata?.ui_kind !== 'proactive_notification'" class="user-turn run-objective-turn">
            <div class="message-bubble">{{ store.currentRun.objective }}</div>
          </div>
          <AgentRunTurn
            :answer="currentRunAssistant?.content || ''"
            :user-message="currentRunUser"
            :message-id="currentRunAssistant?.id"
            :message-metadata="currentRunAssistant?.message_metadata || {}"
            :highlighted="currentRunAssistant?.id === store.highlightedMessageId"
          />
        </template>

      </div>
    </div>

    <AgentComposer />
  </section>
</template>
