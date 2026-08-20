import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client.js';
import { createLoadFence, errorMessage, removeById, replaceById, sameId } from './helpers.js';

export const useSessionStore = defineStore('session', () => {
  const sessions = ref([]);
  const archivedSessions = ref([]);
  const activeSessionId = ref(null);
  const conversationMessages = ref([]);
  const planningState = ref({ intake: null, proposal: null });
  const queuedMessages = ref([]);
  const loading = ref({});
  const errors = ref({});
  const activeFence = createLoadFence();
  const archiveFence = createLoadFence();
  const conversationFence = createLoadFence();
  const queueFence = createLoadFence();

  const activeSession = computed(() => [...sessions.value, ...archivedSessions.value].find(
    (session) => sameId(session.id, activeSessionId.value),
  ) || null);

  async function loadCollection(key, path, target, fence) {
    const generation = fence.next();
    loading.value = { ...loading.value, [key]: true };
    try {
      const response = await api.get(path);
      if (fence.current(generation)) target.value = response.data;
      return response.data;
    } catch (error) {
      if (fence.current(generation)) errors.value = { ...errors.value, [key]: errorMessage(error) };
      throw error;
    } finally {
      if (fence.current(generation)) loading.value = { ...loading.value, [key]: false };
    }
  }

  const loadActiveSessions = () => loadCollection('active', '/agent/sessions', sessions, activeFence);
  const loadArchivedSessions = () => loadCollection('archived', '/agent/sessions?archived=true', archivedSessions, archiveFence);

  async function loadConversation(sessionId) {
    const generation = conversationFence.next();
    if (!sessionId) {
      conversationMessages.value = [];
      planningState.value = { intake: null, proposal: null };
      return [];
    }
    loading.value = { ...loading.value, conversation: true, planning: true };
    const messagesRequest = api.get(`/agent/sessions/${sessionId}/messages`)
      .then((response) => {
        if (conversationFence.current(generation) && sameId(activeSessionId.value, sessionId)) {
          conversationMessages.value = response.data;
        }
        return response.data;
      })
      .catch((error) => {
        if (conversationFence.current(generation)) errors.value = { ...errors.value, conversation: errorMessage(error) };
        throw error;
      })
      .finally(() => {
        if (conversationFence.current(generation)) loading.value = { ...loading.value, conversation: false };
      });
    api.get(`/agent/sessions/${sessionId}/planning`)
      .then((response) => {
        if (conversationFence.current(generation) && sameId(activeSessionId.value, sessionId)) {
          planningState.value = response.data;
        }
      })
      .catch((error) => {
        if (conversationFence.current(generation)) errors.value = { ...errors.value, planning: errorMessage(error) };
      })
      .finally(() => {
        if (conversationFence.current(generation)) loading.value = { ...loading.value, planning: false };
      });
    return messagesRequest;
  }

  async function loadQueue() {
    const generation = queueFence.next();
    const params = activeSessionId.value ? `?session_id=${encodeURIComponent(activeSessionId.value)}` : '';
    const response = await api.get(`/agent/queue${params}`);
    if (queueFence.current(generation)) queuedMessages.value = response.data || [];
    return response.data || [];
  }

  async function enqueueMessage(objective, planId = null) {
    const response = await api.post('/agent/queue', {
      objective,
      session_id: activeSessionId.value || null,
      plan_id: planId,
    });
    await loadQueue();
    return response.data;
  }

  async function updateQueuedMessage(messageId, patch) {
    const current = queuedMessages.value.find((item) => sameId(item.id, messageId));
    await api.patch(`/agent/queue/${messageId}`, { ...patch, expected_version: current?.version ?? 1 });
    await loadQueue();
  }

  async function deleteQueuedMessage(messageId) {
    const current = queuedMessages.value.find((item) => sameId(item.id, messageId));
    await api.delete(`/agent/queue/${messageId}`, { expected_version: current?.version ?? 1 });
    await loadQueue();
  }

  async function moveQueuedMessage(messageId, direction) {
    const index = queuedMessages.value.findIndex((item) => sameId(item.id, messageId));
    const target = index + direction;
    if (index < 0 || target < 0 || target >= queuedMessages.value.length) return;
    await updateQueuedMessage(messageId, { position: queuedMessages.value[target].position });
  }

  async function renameSession(sessionId, title) {
    const response = await api.patch(`/agent/sessions/${sessionId}`, { title });
    sessions.value = replaceById(sessions.value, response.data);
    archivedSessions.value = replaceById(archivedSessions.value, response.data);
    return response.data;
  }

  async function setArchived(sessionId, archived) {
    const response = await api.patch(`/agent/sessions/${sessionId}`, { archived });
    const session = response.data;
    if (archived) {
      sessions.value = removeById(sessions.value, sessionId);
      archivedSessions.value = replaceById(archivedSessions.value, session);
    } else {
      archivedSessions.value = removeById(archivedSessions.value, sessionId);
      sessions.value = replaceById(sessions.value, session);
    }
    void Promise.allSettled([loadActiveSessions(), loadArchivedSessions()]);
    return session;
  }

  function setPlanningState(value) { planningState.value = value; }
  function resetConversation() {
    conversationFence.next();
    activeSessionId.value = null;
    conversationMessages.value = [];
    planningState.value = { intake: null, proposal: null };
  }

  return {
    sessions,
    archivedSessions,
    activeSessionId,
    activeSession,
    conversationMessages,
    planningState,
    queuedMessages,
    loading,
    errors,
    loadActiveSessions,
    loadArchivedSessions,
    loadConversation,
    loadQueue,
    enqueueMessage,
    updateQueuedMessage,
    deleteQueuedMessage,
    moveQueuedMessage,
    renameSession,
    setArchived,
    setPlanningState,
    resetConversation,
  };
});
