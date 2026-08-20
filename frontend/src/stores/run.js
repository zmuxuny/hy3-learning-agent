import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client.js';
import { createLoadFence } from './helpers.js';
import { isRunStreamable } from '../runState.js';

const RUN_EVENTS = [
  'run.started', 'run.resumed', 'run.retrying', 'context.built', 'assistant.status',
  'assistant.message', 'assistant.reasoning', 'assistant.delta', 'steer.received',
  'tool.started', 'tool.completed', 'approval.required', 'approval.resolved',
  'operation.committed', 'notification.sent', 'subagent.started', 'subagent.completed',
  'subagent.cancelled', 'run.budget_exceeded', 'run.completed', 'run.failed', 'run.cancelled',
];

function normalizeEvents(events) {
  return events.map((event) => ({
    sequence: event.sequence,
    type: event.event_type ?? event.type,
    summary: event.summary,
    payload: event.payload,
    created_at: event.created_at,
  }));
}

export const useRunStore = defineStore('run', () => {
  const runs = ref([]);
  const operations = ref([]);
  const currentRun = ref(null);
  const streamingText = ref('');
  const streamingReasoning = ref('');
  const streamingRunId = ref(null);
  const runEvents = ref([]);
  const runEventCache = ref({});
  const runEventLoading = ref({});
  const loading = ref({});
  const errors = ref({});
  const runsFence = createLoadFence();
  const operationsFence = createLoadFence();
  let eventSource = null;
  let handlers = {};
  let streamGeneration = 0;
  let reconciliation = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;

  const activeSubagents = computed(() => {
    if (!currentRun.value) return [];
    return runEvents.value
      .filter((event) => event.type === 'subagent.started')
      .map((event) => {
        const childId = event.payload?.child_run_id;
        const terminal = [...runEvents.value].reverse().find((item) => (
          ['subagent.completed', 'subagent.cancelled'].includes(item.type)
          && item.payload?.child_run_id === childId
        ));
        return {
          child_run_id: childId,
          role: event.payload?.role || '子 Agent',
          objective: event.payload?.objective || '',
          status: terminal ? 'done' : 'running',
        };
      })
      .filter((agent) => agent.status === 'running');
  });

  function setEventHandlers(nextHandlers) { handlers = nextHandlers || {}; }

  async function loadRuns() {
    const generation = runsFence.next();
    loading.value = { ...loading.value, runs: true };
    try {
      const response = await api.get('/agent/runs');
      if (runsFence.current(generation)) runs.value = response.data;
      return response.data;
    } finally {
      if (runsFence.current(generation)) loading.value = { ...loading.value, runs: false };
    }
  }

  async function loadOperations() {
    const generation = operationsFence.next();
    loading.value = { ...loading.value, operations: true };
    try {
      const response = await api.get('/operations');
      if (operationsFence.current(generation)) operations.value = response.data;
      return response.data;
    } finally {
      if (operationsFence.current(generation)) loading.value = { ...loading.value, operations: false };
    }
  }

  function runForId(runId) {
    if (!runId) return null;
    if (currentRun.value?.id === runId) return currentRun.value;
    return runs.value.find((run) => run.id === runId) || null;
  }

  function eventsForRun(runId) {
    if (!runId) return [];
    return currentRun.value?.id === runId ? runEvents.value : (runEventCache.value[runId] || []);
  }

  async function loadRunEvents(runId, force = false) {
    if (!runId) return [];
    if (!force && runEventCache.value[runId]) return runEventCache.value[runId];
    if (runEventLoading.value[runId]) return runEventLoading.value[runId];
    const request = api.get(`/agent/runs/${runId}/events`)
      .then((response) => normalizeEvents(response.data))
      .then((events) => {
        runEventCache.value = { ...runEventCache.value, [runId]: events };
        return events;
      })
      .finally(() => {
        const next = { ...runEventLoading.value };
        delete next[runId];
        runEventLoading.value = next;
      });
    runEventLoading.value = { ...runEventLoading.value, [runId]: request };
    return request;
  }

  function prependRun(run) {
    runs.value = [run, ...runs.value.filter((item) => item.id !== run.id)];
    currentRun.value = run;
  }

  function closeEventSource() {
    streamGeneration += 1;
    if (reconnectTimer != null) globalThis.clearTimeout(reconnectTimer);
    reconnectTimer = null;
    reconciliation = null;
    eventSource?.close();
    eventSource = null;
  }

  function resetCurrentRun() {
    closeEventSource();
    currentRun.value = null;
    runEvents.value = [];
    streamingRunId.value = null;
    streamingText.value = '';
    streamingReasoning.value = '';
  }

  function subscribeToRun(runId, clear = true) {
    closeEventSource();
    if (clear) runEvents.value = [];
    reconnectAttempt = 0;
    const generation = streamGeneration;
    const source = new EventSource(`/api/v1/agent/runs/${runId}/events/stream`);
    eventSource = source;
    RUN_EVENTS.forEach((eventName) => {
      source.addEventListener(eventName, async (event) => {
        if (generation !== streamGeneration || source !== eventSource) return;
        const payload = JSON.parse(event.data);
        if (payload.sequence != null && !runEvents.value.some((item) => item.sequence === payload.sequence)) {
          runEvents.value.push(payload);
          runEventCache.value = { ...runEventCache.value, [runId]: [...runEvents.value] };
        }
        if (eventName === 'tool.completed') await handlers.onToolCompleted?.(payload);
        if (eventName === 'steer.received') handlers.onSteer?.(payload, runId);
        if (eventName === 'assistant.delta' && payload.payload?.text != null) {
          streamingRunId.value = runId;
          streamingText.value = payload.payload.text;
          streamingReasoning.value = '';
        }
        if (eventName === 'assistant.reasoning' && payload.payload?.text != null) {
          streamingRunId.value = runId;
          streamingReasoning.value = payload.payload.text;
        }
        if (eventName === 'assistant.status' || eventName === 'assistant.message') {
          streamingRunId.value = runId;
          streamingText.value = payload.summary || payload.payload?.content || '';
          streamingReasoning.value = '';
        }
        if (['run.completed', 'run.failed', 'run.cancelled'].includes(eventName)) {
          streamingRunId.value = null;
          streamingText.value = '';
          streamingReasoning.value = '';
          if (currentRun.value?.id === runId) currentRun.value = { ...currentRun.value, status: eventName.split('.')[1] };
          closeEventSource();
          await handlers.onTerminal?.(runId);
        }
      });
    });
    source.onerror = () => {
      if (generation !== streamGeneration || source !== eventSource) return;
      void reconcileStream(runId, generation, source).catch(() => {});
    };
  }

  function mergeEvents(existing, incoming) {
    const bySequence = new Map();
    for (const item of [...existing, ...normalizeEvents(incoming)]) {
      if (item.sequence == null || bySequence.has(item.sequence)) continue;
      bySequence.set(item.sequence, item);
    }
    return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
  }

  function scheduleReconnect(runId, generation, source) {
    if (generation !== streamGeneration || source !== eventSource || source.readyState !== 2) return;
    reconnectAttempt += 1;
    const delay = Math.min(8000, 500 * (2 ** Math.min(reconnectAttempt - 1, 4)));
    reconnectTimer = globalThis.setTimeout(() => {
      if (generation !== streamGeneration || source !== eventSource || currentRun.value?.id !== runId) return;
      subscribeToRun(runId, false);
    }, delay);
  }

  function reconcileStream(runId, generation, source) {
    if (reconciliation?.generation === generation) return reconciliation.promise;
    const sessionId = currentRun.value?.id === runId ? currentRun.value.session_id : null;
    const promise = (async () => {
      const [eventsResult, runResult, messagesResult] = await Promise.allSettled([
        Promise.resolve().then(() => api.get(`/agent/runs/${runId}/events`)),
        Promise.resolve().then(() => api.get(`/agent/runs/${runId}`)),
        sessionId
          ? Promise.resolve().then(() => api.get(`/agent/sessions/${sessionId}/messages`))
          : Promise.resolve({ data: [] }),
      ]);
      if (generation !== streamGeneration || source !== eventSource || currentRun.value?.id !== runId) return;
      if (eventsResult.status === 'fulfilled') {
        runEvents.value = mergeEvents(runEvents.value, eventsResult.value.data);
        runEventCache.value = { ...runEventCache.value, [runId]: [...runEvents.value] };
      }
      if (messagesResult.status === 'fulfilled' && sessionId) {
        await handlers.onMessagesReconciled?.(sessionId, messagesResult.value.data);
      }
      if (runResult.status === 'fulfilled') currentRun.value = runResult.value.data;
      const terminal = runResult.status === 'fulfilled'
        && ['completed', 'failed', 'cancelled'].includes(runResult.value.data.status);
      if (terminal) {
        closeEventSource();
        await handlers.onTerminal?.(runId);
        return;
      }
      scheduleReconnect(runId, generation, source);
    })().finally(() => {
      if (reconciliation?.promise === promise) reconciliation = null;
    });
    reconciliation = { generation, promise };
    return promise;
  }

  async function inspectRun(run) {
    closeEventSource();
    currentRun.value = run;
    runEvents.value = await loadRunEvents(run.id, true);
    if (isRunStreamable(run.status)) subscribeToRun(run.id, false);
    return run;
  }

  async function inspectChildRun(runId) {
    const response = await api.get(`/agent/runs/${runId}`);
    return inspectRun(response.data);
  }

  async function steerRun(runId, content) {
    const response = await api.post(`/agent/runs/${runId}/steer`, { content });
    currentRun.value = response.data;
    return response.data;
  }

  async function cancelCurrentRun() {
    if (!currentRun.value) return null;
    return (await api.post(`/agent/runs/${currentRun.value.id}/cancel`)).data;
  }

  async function decideRunApproval(runId, approved, answer = '') {
    const response = await api.post(`/agent/runs/${runId}/approval`, { approved, answer: answer || undefined });
    currentRun.value = response.data;
    subscribeToRun(runId, false);
    return response.data;
  }

  const fetchChildRunEvents = async (childId) => (await api.get(`/agent/runs/${childId}/events`)).data;
  const fetchRunContext = async (runId) => (await api.get(`/agent/runs/${runId}/context`)).data;

  async function undoOperation(operationId) {
    const response = await api.post(`/operations/${operationId}/undo`);
    void loadOperations();
    return response.data;
  }

  return {
    runs,
    operations,
    currentRun,
    streamingText,
    streamingReasoning,
    streamingRunId,
    runEvents,
    runEventCache,
    runEventLoading,
    loading,
    errors,
    activeSubagents,
    setEventHandlers,
    loadRuns,
    loadOperations,
    runForId,
    eventsForRun,
    loadRunEvents,
    prependRun,
    closeEventSource,
    resetCurrentRun,
    subscribeToRun,
    inspectRun,
    inspectChildRun,
    steerRun,
    cancelCurrentRun,
    decideRunApproval,
    fetchChildRunEvents,
    fetchRunContext,
    undoOperation,
  };
});
