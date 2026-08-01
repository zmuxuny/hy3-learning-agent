import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client';

const RUN_EVENTS = [
  'run.started',
  'run.retrying',
  'context.built',
  'assistant.status',
  'assistant.message',
  'tool.started',
  'tool.completed',
  'approval.required',
  'operation.committed',
  'notification.sent',
  'run.completed',
  'run.failed',
  'run.cancelled',
];

export const useWorkspaceStore = defineStore('workspace', () => {
  const activeView = ref('home');
  const planScreen = ref('list');
  const profile = ref(null);
  const plans = ref([]);
  const archivedPlans = ref([]);
  const dashboard = ref({ activity: [], achievements: [], due_review_count: 0, open_quiz_count: 0 });
  const currentPlan = ref(null);
  const planResources = ref([]);
  const memories = ref([]);
  const notifications = ref([]);
  const operations = ref([]);
  const runs = ref([]);
  const sessions = ref([]);
  const archivedSessions = ref([]);
  const emailConfiguration = ref(null);
  const emailTestResult = ref(null);
  const currentRun = ref(null);
  const focusPlanId = ref(null);
  const traceOpen = ref(false);
  const activeSessionId = ref(null);
  const conversationMessages = ref([]);
  const runEvents = ref([]);
  const loading = ref(false);
  const error = ref('');
  let eventSource = null;

  const unreadCount = computed(() => notifications.value.filter((item) => !item.read_at).length);
  const activePlans = computed(() => plans.value.filter((plan) => plan.status === 'active'));
  const pendingMemories = computed(() => memories.value.filter((memory) => memory.status === 'proposed'));
  const focusedPlan = computed(() => (
    [...plans.value, ...archivedPlans.value].find((plan) => plan.id === focusPlanId.value) || null
  ));
  const activeSession = computed(() => (
    [...sessions.value, ...archivedSessions.value].find((session) => session.id === activeSessionId.value) || null
  ));
  const createdPlanFromCurrentRun = computed(() => {
    if (currentRun.value?.plan_id != null) return null;
    const event = [...runEvents.value].reverse().find((item) => (
      item.type === 'tool.completed'
      && item.payload?.name === 'plan_create'
      && item.payload?.result?.ok
    ));
    const data = event?.payload?.result?.data;
    if (!data?.plan_id) return null;
    const plan = plans.value.find((item) => Number(item.id) === Number(data.plan_id));
    return plan || { id: Number(data.plan_id), title: data.title || `计划 ${data.plan_id}` };
  });

  async function loadWorkspace() {
    loading.value = true;
    error.value = '';
    try {
      const [profileRes, plansRes, archivedPlansRes, dashboardRes, memoriesRes, notificationsRes, operationsRes, runsRes, sessionsRes, archivedSessionsRes, emailRes] = await Promise.all([
        api.get('/profile'),
        api.get('/plans'),
        api.get('/plans?archived=true'),
        api.get('/dashboard'),
        api.get('/memories'),
        api.get('/notifications'),
        api.get('/operations'),
        api.get('/agent/runs'),
        api.get('/agent/sessions'),
        api.get('/agent/sessions?archived=true'),
        api.get('/settings/email'),
      ]);
      profile.value = profileRes.data;
      plans.value = plansRes.data;
      archivedPlans.value = archivedPlansRes.data;
      dashboard.value = dashboardRes.data;
      memories.value = memoriesRes.data;
      notifications.value = notificationsRes.data;
      operations.value = operationsRes.data;
      runs.value = runsRes.data;
      sessions.value = sessionsRes.data;
      archivedSessions.value = archivedSessionsRes.data;
      emailConfiguration.value = emailRes.data;
      await refreshCurrentPlan();
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
    } finally {
      loading.value = false;
    }
  }

  async function loadPlan(planId) {
    const [planResponse, resourcesResponse] = await Promise.all([
      api.get(`/plans/${planId}`),
      api.get(`/plans/${planId}/resources`),
    ]);
    currentPlan.value = planResponse.data;
    planResources.value = resourcesResponse.data;
  }

  async function loadConversation(sessionId) {
    if (!sessionId) {
      conversationMessages.value = [];
      return;
    }
    const response = await api.get(`/agent/sessions/${sessionId}/messages`);
    if (activeSessionId.value === sessionId) conversationMessages.value = response.data;
  }

  async function loadSessions() {
    const [activeResponse, archivedResponse] = await Promise.all([
      api.get('/agent/sessions'),
      api.get('/agent/sessions?archived=true'),
    ]);
    sessions.value = activeResponse.data;
    archivedSessions.value = archivedResponse.data;
  }

  async function refreshCurrentPlan() {
    if (!plans.value.length) {
      currentPlan.value = null;
      planResources.value = [];
      focusPlanId.value = null;
      planScreen.value = 'list';
      return;
    }
    if (!currentPlan.value) return;
    const selectedPlan = plans.value.find((plan) => plan.id === currentPlan.value.id);
    if (!selectedPlan) {
      if (focusPlanId.value === currentPlan.value.id) focusPlanId.value = null;
      currentPlan.value = null;
      planResources.value = [];
      planScreen.value = 'list';
      return;
    }
    await loadPlan(selectedPlan.id);
  }

  async function selectPlan(planId) {
    await loadPlan(planId);
    if (focusPlanId.value !== planId) resetConversationState();
    planScreen.value = 'detail';
    focusPlanId.value = planId;
    activeView.value = 'plans';
  }

  function openPlanList() {
    planScreen.value = 'list';
    activeView.value = 'plans';
  }

  async function openView(view) {
    if (view === 'plans') planScreen.value = 'list';
    activeView.value = view;
  }

  async function startRun(objective, planId = undefined) {
    if (!objective.trim()) return false;
    const resolvedPlanId = planId === undefined ? focusPlanId.value : planId;
    closeEventSource();
    runEvents.value = [];
    error.value = '';
    try {
      const response = await api.post('/agent/runs', {
        objective,
        plan_id: resolvedPlanId,
        session_id: activeSessionId.value,
        trigger: 'user_message',
      });
      currentRun.value = response.data;
      focusPlanId.value = response.data.plan_id ?? resolvedPlanId ?? null;
      activeSessionId.value = response.data.session_id;
      if (!conversationMessages.value.some((message) => message.run_id === response.data.id && message.role === 'user')) {
        conversationMessages.value.push({
          id: `pending-${response.data.id}`,
          session_id: response.data.session_id,
          run_id: response.data.id,
          role: 'user',
          content: objective,
          message_metadata: {},
          created_at: new Date().toISOString(),
          pending: true,
        });
      }
      activeView.value = 'home';
      runs.value.unshift(response.data);
      await loadSessions();
      subscribeToRun(response.data.id);
      return true;
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
      return false;
    }
  }

  async function triggerHeartbeat() {
    closeEventSource();
    runEvents.value = [];
    error.value = '';
    try {
      const response = await api.post('/agent/heartbeat');
      currentRun.value = response.data;
      focusPlanId.value = response.data.plan_id ?? null;
      activeSessionId.value = response.data.session_id || null;
      conversationMessages.value = [];
      activeView.value = 'home';
      runs.value.unshift(response.data);
      subscribeToRun(response.data.id);
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
    }
  }

  async function inspectRun(run) {
    closeEventSource();
    currentRun.value = run;
    focusPlanId.value = run.plan_id ?? null;
    activeSessionId.value = run.session_id || null;
    activeView.value = 'home';
    const [response] = await Promise.all([
      api.get(`/agent/runs/${run.id}/events`),
      loadConversation(activeSessionId.value),
    ]);
    runEvents.value = response.data.map((event) => ({
      sequence: event.sequence,
      type: event.event_type,
      summary: event.summary,
      payload: event.payload,
      created_at: event.created_at,
    }));
    if (['queued', 'running'].includes(run.status)) subscribeToRun(run.id, false);
  }

  async function selectSession(session) {
    closeEventSource();
    activeSessionId.value = session.id;
    focusPlanId.value = session.plan_id ?? null;
    activeView.value = 'home';
    await loadConversation(session.id);
    let run = runs.value.find((item) => item.id === session.last_run_id);
    if (!run && session.last_run_id) {
      run = (await api.get(`/agent/runs/${session.last_run_id}`)).data;
    }
    currentRun.value = run || null;
    if (!run) {
      runEvents.value = [];
      return;
    }
    const response = await api.get(`/agent/runs/${run.id}/events`);
    runEvents.value = response.data.map((event) => ({
      sequence: event.sequence,
      type: event.event_type,
      summary: event.summary,
      payload: event.payload,
      created_at: event.created_at,
    }));
    if (['queued', 'running'].includes(run.status)) subscribeToRun(run.id, false);
  }

  async function renameSession(sessionId, title) {
    const response = await api.patch(`/agent/sessions/${sessionId}`, { title });
    for (const collection of [sessions, archivedSessions]) {
      const index = collection.value.findIndex((session) => session.id === sessionId);
      if (index >= 0) collection.value[index] = response.data;
    }
    return response.data;
  }

  async function setSessionArchived(sessionId, archived) {
    await api.patch(`/agent/sessions/${sessionId}`, { archived });
    if (archived && activeSessionId.value === sessionId) startNewConversation();
    await loadSessions();
  }

  async function continueInPlan(planId) {
    if (!activeSessionId.value) return false;
    const response = await api.post(`/agent/sessions/${activeSessionId.value}/handoff`, { plan_id: planId });
    await loadSessions();
    const session = sessions.value.find((item) => item.id === response.data.id) || response.data;
    await selectSession(session);
    return true;
  }

  async function setPlanArchived(planId, archived) {
    await api.patch(`/plans/${planId}/archive`, { archived });
    const [activeResponse, archivedResponse] = await Promise.all([
      api.get('/plans'),
      api.get('/plans?archived=true'),
    ]);
    plans.value = activeResponse.data;
    archivedPlans.value = archivedResponse.data;
    if (archived && currentPlan.value?.id === planId) {
      currentPlan.value = null;
      planResources.value = [];
      if (focusPlanId.value === planId) resetConversationState();
      planScreen.value = 'list';
    } else if (!archived && currentPlan.value?.id === planId) {
      await loadPlan(planId);
    }
  }

  async function testEmail(channel, sendMessage = false) {
    emailTestResult.value = null;
    try {
      const response = await api.post('/settings/email/test', { channel, send_message: sendMessage });
      emailTestResult.value = response.data;
      const configuration = await api.get('/settings/email');
      emailConfiguration.value = configuration.data;
      return true;
    } catch (requestError) {
      emailTestResult.value = { ok: false, error: requestError.response?.data?.detail || requestError.message };
      return false;
    }
  }

  function subscribeToRun(runId, clear = true) {
    if (clear) runEvents.value = [];
    eventSource = new EventSource(`/api/v1/agent/runs/${runId}/events/stream`);
    RUN_EVENTS.forEach((eventName) => {
      eventSource.addEventListener(eventName, async (event) => {
        const payload = JSON.parse(event.data);
        if (!runEvents.value.some((item) => item.sequence === payload.sequence)) {
          runEvents.value.push(payload);
        }
        if (eventName === 'run.completed' || eventName === 'run.failed' || eventName === 'run.cancelled') {
          currentRun.value = { ...currentRun.value, status: eventName.split('.')[1] };
          closeEventSource();
          await refreshAfterRun();
        }
      });
    });
    eventSource.onerror = () => closeEventSource();
  }

  async function cancelCurrentRun() {
    if (!currentRun.value) return;
    await api.post(`/agent/runs/${currentRun.value.id}/cancel`);
  }

  async function confirmMemory(memoryId) {
    await api.post(`/memories/${memoryId}/confirm`);
    const response = await api.get('/memories');
    memories.value = response.data;
  }

  async function deleteMemory(memoryId) {
    await api.delete(`/memories/${memoryId}`);
    memories.value = memories.value.filter((memory) => memory.id !== memoryId);
  }

  async function markNotificationRead(notificationId) {
    const response = await api.post(`/notifications/${notificationId}/read`);
    const index = notifications.value.findIndex((item) => item.id === notificationId);
    if (index >= 0) notifications.value[index] = response.data;
  }

  async function undoOperation(operationId) {
    await api.post(`/operations/${operationId}/undo`);
    await refreshAfterRun();
  }

  async function refreshAfterRun() {
    const knownNotificationIds = new Set(notifications.value.map((item) => item.id));
    const [profileRes, plansRes, archivedPlansRes, dashboardRes, memoriesRes, notificationsRes, operationsRes, runsRes, sessionsRes, archivedSessionsRes, emailRes] = await Promise.all([
      api.get('/profile'),
      api.get('/plans'),
      api.get('/plans?archived=true'),
      api.get('/dashboard'),
      api.get('/memories'),
      api.get('/notifications'),
      api.get('/operations'),
      api.get('/agent/runs'),
      api.get('/agent/sessions'),
      api.get('/agent/sessions?archived=true'),
      api.get('/settings/email'),
    ]);
    profile.value = profileRes.data;
    plans.value = plansRes.data;
    archivedPlans.value = archivedPlansRes.data;
    dashboard.value = dashboardRes.data;
    memories.value = memoriesRes.data;
    notifications.value = notificationsRes.data;
    if ('Notification' in window && Notification.permission === 'granted') {
      notifications.value
        .filter((item) => item.channel === 'browser' && item.status === 'sent' && !knownNotificationIds.has(item.id))
        .forEach((item) => new Notification(item.title, { body: item.body }));
    }
    operations.value = operationsRes.data;
    runs.value = runsRes.data;
    sessions.value = sessionsRes.data;
    archivedSessions.value = archivedSessionsRes.data;
    emailConfiguration.value = emailRes.data;
    await refreshCurrentPlan();
    await loadConversation(activeSessionId.value);
  }

  function closeEventSource() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  function resetConversationState() {
    activeSessionId.value = null;
    currentRun.value = null;
    conversationMessages.value = [];
    runEvents.value = [];
    closeEventSource();
  }

  function startNewConversation() {
    resetConversationState();
    focusPlanId.value = null;
    activeView.value = 'home';
  }

  return {
    activeView,
    planScreen,
    profile,
    plans,
    archivedPlans,
    dashboard,
    currentPlan,
    planResources,
    memories,
    notifications,
    operations,
    runs,
    sessions,
    archivedSessions,
    emailConfiguration,
    emailTestResult,
    currentRun,
    focusPlanId,
    traceOpen,
    activeSessionId,
    conversationMessages,
    runEvents,
    loading,
    error,
    unreadCount,
    activePlans,
    pendingMemories,
    focusedPlan,
    activeSession,
    createdPlanFromCurrentRun,
    loadWorkspace,
    selectPlan,
    openPlanList,
    openView,
    startRun,
    triggerHeartbeat,
    inspectRun,
    selectSession,
    renameSession,
    setSessionArchived,
    continueInPlan,
    setPlanArchived,
    testEmail,
    cancelCurrentRun,
    confirmMemory,
    deleteMemory,
    markNotificationRead,
    undoOperation,
    startNewConversation,
  };
});
