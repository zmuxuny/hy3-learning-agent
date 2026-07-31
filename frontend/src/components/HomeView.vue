<script setup>
import { BoltIcon, MapIcon, SparklesIcon } from '@heroicons/vue/24/outline';
import { computed, nextTick, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentComposer from './AgentComposer.vue';
import AgentRunTurn from './AgentRunTurn.vue';

const store = useWorkspaceStore();
const scrollArea = ref(null);
const pinnedToBottom = ref(true);
const days = computed(() => store.dashboard.activity.map((day, index) => ({
  index,
  date: day.date,
  value: Math.min(day.count, 4),
})));
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
</script>

<template>
  <section class="conversation-page">
    <div ref="scrollArea" class="conversation-scroll" @scroll="onScroll">
      <div v-if="store.error" class="error-banner">{{ store.error }}</div>

      <div v-if="!store.currentRun && !store.conversationMessages.length" class="welcome-state">
        <div class="welcome-mark"><SparklesIcon /></div>
        <h1>今天想学什么？</h1>
        <p>告诉我你的目标。我会制定计划、持续跟进，并在需要时主动提醒或考核。</p>
        <div class="suggestion-list">
          <button v-for="suggestion in suggestions" :key="suggestion" @click="store.startRun(suggestion)">
            {{ suggestion }}
          </button>
        </div>

        <div class="quiet-overview">
          <div class="overview-copy">
            <span><BoltIcon /> 主动教练已就绪</span>
            <p>后台心跳会自行判断何时介入；没有必要时，它会保持安静。</p>
          </div>
          <div class="overview-stats">
            <button @click="store.openView('plans')"><strong>{{ store.activePlans.length }}</strong><span>进行中计划</span></button>
            <button @click="store.openView('inbox')"><strong>{{ store.unreadCount }}</strong><span>待处理消息</span></button>
            <div class="mini-heatmap" title="最近 12 周真实学习活动">
              <i v-for="day in days" :key="day.index" :class="`heat-${day.value}`"></i>
            </div>
          </div>
        </div>
      </div>

      <div v-else class="thread">
        <template v-for="message in store.conversationMessages" :key="message.id">
          <div v-if="message.role === 'user'" class="user-turn">
            <div class="message-bubble">{{ message.content }}</div>
          </div>

          <AgentRunTurn
            v-if="message.role === 'user' && message.run_id === store.currentRun?.id"
            :answer="currentRunAssistant?.content || ''"
          />

          <div
            v-if="message.role === 'assistant' && message.run_id !== store.currentRun?.id"
            class="agent-turn conversation-agent"
          >
            <div class="agent-avatar"><SparklesIcon /></div>
            <div class="agent-content">
              <div class="agent-name">Learning Agent <span>Hy3</span></div>
              <div class="assistant-answer"><p>{{ message.content }}</p></div>
            </div>
          </div>
        </template>

        <template v-if="store.currentRun && !currentRunUser">
          <div class="user-turn run-objective-turn">
            <div class="message-bubble">{{ store.currentRun.objective }}</div>
          </div>
          <AgentRunTurn :answer="currentRunAssistant?.content || ''" />
        </template>

        <div v-if="store.focusedPlan" class="context-chip"><MapIcon /> 本次对话专注：{{ store.focusedPlan.title }}</div>
      </div>
    </div>

    <AgentComposer />
  </section>
</template>
