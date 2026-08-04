import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client';

const RUN_EVENTS = [
  'run.started',
  'run.resumed',
  'run.retrying',
  'context.built',
  'assistant.status',
  'assistant.message',
  'assistant.reasoning',
  'assistant.delta',
  'steer.received',
  'tool.started',
  'tool.completed',
  'approval.required',
  'approval.resolved',
  'operation.committed',
  'notification.sent',
  'subagent.started',
  'subagent.completed',
  'run.completed',
  'run.failed',
  'run.cancelled',
];

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
  return output;
}

async function showBrowserNotification(title, body) {
  if ('serviceWorker' in navigator) {
    try {
      const registration = await navigator.serviceWorker.ready;
      await registration.showNotification(title, { body, data: { url: '/?view=inbox' } });
      return;
    } catch {
      // Fall through to the page-level Notification API.
    }
  }
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body });
  }
}

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
  const archivedNotifications = ref([]);
  const operations = ref([]);
  const runs = ref([]);
  const sessions = ref([]);
  const archivedSessions = ref([]);
  const emailConfiguration = ref(null);
  const appSettings = ref(null);
  const emailTestResult = ref(null);
  const schedulerStatus = ref(null);
  const proactiveNotice = ref(null);
  const currentRun = ref(null);
  const focusPlanId = ref(null);
  const traceOpen = ref(false);
  const activeSessionId = ref(null);
  const conversationMessages = ref([]);
  const planningState = ref({ intake: null, proposal: null });
  const queuedMessages = ref([]);
  const followUpBehavior = ref('steer');
  const streamingText = ref('');
  const streamingReasoning = ref('');
  const streamingRunId = ref(null);
  const runEvents = ref([]);
  const loading = ref(false);
  const error = ref('');
  const drainingQueue = ref(false);
  let eventSource = null;
  let proactiveTimer = null;

  const unreadCount = computed(() => notifications.value.filter((item) => !item.read_at).length);
  const activePlans = computed(() => plans.value.filter((plan) => plan.status === 'active'));
  const pendingMemories = computed(() => memories.value.filter((memory) => memory.status === 'proposed'));
  const focusedPlan = computed(() => (
    [...plans.value, ...archivedPlans.value].find((plan) => plan.id === focusPlanId.value) || null
  ));
  const activeSession = computed(() => (
    [...sessions.value, ...archivedSessions.value].find((session) => session.id === activeSessionId.value) || null
  ));
  const activeSubagents = computed(() => {
    if (!currentRun.value) return [];
    const started = runEvents.value.filter((event) => event.type === 'subagent.started');
    return started
      .map((event) => {
        const childId = event.payload?.child_run_id;
        const terminal = [...runEvents.value].reverse().find(
          (item) => (
            item.type === 'subagent.completed' || item.type === 'subagent.cancelled'
          ) && item.payload?.child_run_id === childId,
        );
        return {
          child_run_id: childId,
          role: event.payload?.role || '子 Agent',
          objective: event.payload?.objective || '',
          status: terminal ? 'done' : 'running',
        };
      })
      .filter((agent) => agent.status === 'running');
  });
  const createdPlanFromCurrentRun = computed(() => {
    if (planningState.value.proposal?.status === 'accepted' && planningState.value.proposal?.plan_id) {
      const proposal = planningState.value.proposal;
      return plans.value.find((item) => Number(item.id) === Number(proposal.plan_id))
        || { id: Number(proposal.plan_id), title: proposal.title };
    }
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

  function planForRun(runId) {
    const run = runs.value.find((item) => item.id === runId);
    if (!run?.created_plan_id) return null;
    return [...plans.value, ...archivedPlans.value].find(
      (plan) => Number(plan.id) === Number(run.created_plan_id),
    ) || { id: Number(run.created_plan_id), title: `计划 ${run.created_plan_id}` };
  }

  async function loadWorkspace() {
    loading.value = true;
    error.value = '';
    try {
      const [profileRes, plansRes, archivedPlansRes, dashboardRes, memoriesRes, notificationsRes, archivedNotificationsRes, operationsRes, runsRes, sessionsRes, archivedSessionsRes, emailRes, proactiveRes, settingsRes, followupRes, queueRes] = await Promise.all([
        api.get('/profile'),
        api.get('/plans'),
        api.get('/plans?archived=true'),
        api.get('/dashboard'),
        api.get('/memories'),
        api.get('/notifications'),
        api.get('/notifications?archived=true'),
        api.get('/operations'),
        api.get('/agent/runs'),
        api.get('/agent/sessions'),
        api.get('/agent/sessions?archived=true'),
        api.get('/settings/email'),
        api.get('/settings/proactive'),
        api.get('/settings'),
        api.get('/settings/followup'),
        api.get('/agent/queue'),
      ]);
      profile.value = profileRes.data;
      plans.value = plansRes.data;
      archivedPlans.value = archivedPlansRes.data;
      dashboard.value = dashboardRes.data;
      memories.value = memoriesRes.data;
      notifications.value = notificationsRes.data;
      archivedNotifications.value = archivedNotificationsRes.data;
      operations.value = operationsRes.data;
      runs.value = runsRes.data;
      sessions.value = sessionsRes.data;
      archivedSessions.value = archivedSessionsRes.data;
      emailConfiguration.value = emailRes.data;
      appSettings.value = settingsRes.data;
      schedulerStatus.value = proactiveRes.data;
      followUpBehavior.value = followupRes.data?.follow_up_behavior || 'steer';
      queuedMessages.value = queueRes.data || [];
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
      planningState.value = { intake: null, proposal: null };
      return;
    }
    const [response, planningResponse] = await Promise.all([
      api.get(`/agent/sessions/${sessionId}/messages`),
      api.get(`/agent/sessions/${sessionId}/planning`),
    ]);
    if (activeSessionId.value === sessionId) {
      conversationMessages.value = response.data;
      planningState.value = planningResponse.data;
    }
  }

  async function loadQueue() {
    const params = activeSessionId.value ? `?session_id=${encodeURIComponent(activeSessionId.value)}` : '';
    const response = await api.get(`/agent/queue${params}`);
    queuedMessages.value = response.data;
  }

  async function enqueueMessage(objective) {
    const response = await api.post('/agent/queue', {
      objective,
      session_id: activeSessionId.value || null,
      plan_id: focusPlanId.value ?? null,
    });
    await loadQueue();
    return response.data;
  }

  async function updateQueuedMessage(messageId, patch) {
    await api.patch(`/agent/queue/${messageId}`, patch);
    await loadQueue();
  }

  async function deleteQueuedMessage(messageId) {
    await api.delete(`/agent/queue/${messageId}`);
    await loadQueue();
  }

  async function moveQueuedMessage(messageId, direction) {
    const index = queuedMessages.value.findIndex((item) => item.id === messageId);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= queuedMessages.value.length) return;
    const targetMessage = queuedMessages.value[target];
    await updateQueuedMessage(messageId, { position: targetMessage.position });
    await updateQueuedMessage(targetMessage.id, { position: queuedMessages.value[index].position });
  }

  async function sendQueuedMessage(messageId) {
    closeEventSource();
    runEvents.value = [];
    error.value = '';
    try {
      const response = await api.post(`/agent/queue/${messageId}/send`);
      currentRun.value = response.data;
      focusPlanId.value = response.data.plan_id ?? focusPlanId.value;
      activeSessionId.value = response.data.session_id || activeSessionId.value;
      activeView.value = 'home';
      runs.value.unshift(response.data);
      await loadSessions();
      await loadQueue();
      subscribeToRun(response.data.id);
      return true;
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
      return false;
    }
  }

  async function steerRun(runId, content) {
    try {
      const response = await api.post(`/agent/runs/${runId}/steer`, { content });
      currentRun.value = response.data;
      return true;
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
      return false;
    }
  }

  async function setFollowUpBehavior(behavior) {
    const response = await api.put('/settings/followup', { follow_up_behavior: behavior });
    followUpBehavior.value = response.data?.follow_up_behavior || behavior;
    return response.data;
  }

  async function setProactivePaused(paused) {
    const response = await api.put('/settings/proactive', { paused });
    schedulerStatus.value = response.data;
    return response.data;
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

  async function startRun(objective, planId = undefined, options = {}) {
    if (!objective.trim()) return false;
    const resolvedPlanId = planId === undefined ? focusPlanId.value : planId;
    if (options.mode === 'queue' && currentRun.value && ['queued', 'running', 'waiting_approval'].includes(currentRun.value.status)) {
      await enqueueMessage(objective);
      return 'queued';
    }
    if (options.mode === 'interrupt' && currentRun.value && ['queued', 'running'].includes(currentRun.value.status)) {
      await api.post(`/agent/runs/${currentRun.value.id}/cancel`);
      const deadline = Date.now() + 8000;
      while (Date.now() < deadline) {
        const polled = (await api.get(`/agent/runs/${currentRun.value.id}`)).data;
        currentRun.value = polled;
        if (!['queued', 'running'].includes(polled.status)) break;
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
    }
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
      await refreshProactiveState();
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
      loadQueue(),
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
    await loadQueue();
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

  async function editMessage(messageId, content) {
    closeEventSource();
    runEvents.value = [];
    error.value = '';
    try {
      const response = await api.post(`/agent/messages/${messageId}/edit`, { content, rerun: true });
      currentRun.value = response.data;
      activeSessionId.value = response.data.session_id;
      focusPlanId.value = response.data.plan_id ?? null;
      await loadConversation(activeSessionId.value);
      await loadSessions();
      subscribeToRun(response.data.id);
      return true;
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
      return false;
    }
  }

  async function submitPlanningAnswers(answers) {
    if (!activeSessionId.value || !answers.length) return false;
    closeEventSource();
    runEvents.value = [];
    error.value = '';
    try {
      const response = await api.post(`/agent/sessions/${activeSessionId.value}/planning/answers`, { answers });
      currentRun.value = response.data;
      const intake = planningState.value.intake;
      if (intake) {
        planningState.value = {
          ...planningState.value,
          intake: {
            ...intake,
            open_questions: [],
            readiness: 'collecting',
            rationale: '回答已提交，Agent 正在重新判断需求是否充分。',
          },
        };
      }
      conversationMessages.value.push({
        id: `planning-answers-${response.data.id}`,
        session_id: activeSessionId.value,
        run_id: response.data.id,
        role: 'user',
        content: '',
        message_metadata: { ui_kind: 'planning_answers', answer_count: answers.length },
        created_at: new Date().toISOString(),
        pending: true,
      });
      runs.value.unshift(response.data);
      await loadSessions();
      subscribeToRun(response.data.id);
      return true;
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
      return false;
    }
  }

  async function decidePlanProposal(proposalId, accepted) {
    error.value = '';
    try {
      const response = await api.post(`/agent/plan-proposals/${proposalId}/decision`, { accepted });
      planningState.value = { ...planningState.value, proposal: response.data };
      const [plansResponse, archivedResponse, operationsResponse] = await Promise.all([
        api.get('/plans'),
        api.get('/plans?archived=true'),
        api.get('/operations'),
      ]);
      plans.value = plansResponse.data;
      archivedPlans.value = archivedResponse.data;
      operations.value = operationsResponse.data;
      await loadSessions();
      return true;
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
      return false;
    }
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

  async function updateEmailSettings(payload) {
    const response = await api.put('/settings/email', payload);
    const configuration = await api.get('/settings/email');
    emailConfiguration.value = configuration.data;
    return response.data;
  }

  async function deleteEmailCredentials() {
    const response = await api.delete('/settings/email');
    const configuration = await api.get('/settings/email');
    emailConfiguration.value = configuration.data;
    return response.data;
  }

  async function updateModelSettings(payload) {
    const response = await api.put('/settings/model', payload);
    appSettings.value = { ...(appSettings.value || {}), ...response.data };
    return response.data;
  }

  async function updateNotificationPolicy(payload) {
    return (await api.put('/settings/notification', payload)).data;
  }

  async function refreshProactiveState() {
    try {
      const knownIds = new Set(notifications.value.map((item) => item.id));
      const [notificationsResponse, proactiveResponse] = await Promise.all([
        api.get('/notifications'),
        api.get('/settings/proactive'),
      ]);
      const fresh = notificationsResponse.data.filter((item) => (
        !knownIds.has(item.id) && item.channel === 'in_app' && item.status === 'sent'
      ));
      notifications.value = notificationsResponse.data;
      schedulerStatus.value = proactiveResponse.data;
      if (fresh.length) {
        proactiveNotice.value = fresh[0];
        showBrowserNotification(fresh[0].title, fresh[0].body);
      }
    } catch {
      // Background visibility must never interrupt the active conversation.
    }
  }

  function startProactiveSync() {
    if (proactiveTimer) return;
    proactiveTimer = window.setInterval(refreshProactiveState, 15000);
  }

  function stopProactiveSync() {
    if (proactiveTimer) window.clearInterval(proactiveTimer);
    proactiveTimer = null;
  }

  function dismissProactiveNotice() {
    proactiveNotice.value = null;
  }

  function subscribeToRun(runId, clear = true) {
    if (clear) runEvents.value = [];
    eventSource = new EventSource(`/api/v1/agent/runs/${runId}/events/stream`);
    RUN_EVENTS.forEach((eventName) => {
      eventSource.addEventListener(eventName, async (event) => {
        const payload = JSON.parse(event.data);
        if (payload.sequence != null && !runEvents.value.some((item) => item.sequence === payload.sequence)) {
          runEvents.value.push(payload);
        }
        if (eventName === 'tool.completed') {
          const toolName = payload.payload?.name;
          const result = payload.payload?.result || {};
          if (result.ok && toolName === 'planning_intake_update' && result.data?.open_questions) {
            planningState.value = {
              ...planningState.value,
              intake: { ...result.data, source_run_id: currentRun.value?.id },
            };
          } else if (result.ok && toolName === 'plan_proposal_create') {
            try {
              const planningResponse = await api.get(`/agent/sessions/${activeSessionId.value}/planning`);
              planningState.value = planningResponse.data;
            } catch {
              // The run-completion refresh will reconcile planning state.
            }
          }
        }
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
        if (eventName === 'steer.received') {
          const steerContent = payload.payload?.content || '';
          if (steerContent && !conversationMessages.value.some((message) => (
            message.run_id === runId
            && message.role === 'user'
            && message.message_metadata?.ui_kind === 'steer'
            && message.content === steerContent
          ))) {
            conversationMessages.value.push({
              id: `steer-${payload.sequence}-${payload.payload?.steer_id || ''}`,
              session_id: activeSessionId.value,
              run_id: runId,
              role: 'user',
              content: steerContent,
              message_metadata: { ui_kind: 'steer' },
              created_at: new Date().toISOString(),
            });
          }
        }
        if (eventName === 'run.completed' || eventName === 'run.failed' || eventName === 'run.cancelled') {
          streamingRunId.value = null;
          streamingText.value = '';
          streamingReasoning.value = '';
          currentRun.value = { ...currentRun.value, status: eventName.split('.')[1] };
          closeEventSource();
          await refreshAfterRun();
          const sessionQueue = queuedMessages.value.filter((item) => (
            item.session_id === (activeSessionId.value || null)
          ));
          if (!drainingQueue.value && sessionQueue.length) {
            drainingQueue.value = true;
            try {
              const next = sessionQueue[0];
              await sendQueuedMessage(next.id);
            } finally {
              drainingQueue.value = false;
            }
          }
        }
      });
    });
    eventSource.onerror = () => closeEventSource();
  }

  async function cancelCurrentRun() {
    if (!currentRun.value) return;
    await api.post(`/agent/runs/${currentRun.value.id}/cancel`);
  }

  async function decideRunApproval(runId, approved, answer = '') {
    error.value = '';
    try {
      const response = await api.post(`/agent/runs/${runId}/approval`, {
        approved,
        answer: answer || undefined,
      });
      currentRun.value = response.data;
      subscribeToRun(runId, false);
      return true;
    } catch (requestError) {
      error.value = requestError.response?.data?.detail || requestError.message;
      return false;
    }
  }

  async function fetchChildRunEvents(childId) {
    const response = await api.get(`/agent/runs/${childId}/events`);
    return response.data;
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
    for (const collection of [notifications, archivedNotifications]) {
      const index = collection.value.findIndex((item) => item.id === notificationId);
      if (index >= 0) collection.value[index] = response.data;
    }
  }

  async function refreshNotifications() {
    const [activeResponse, archivedResponse] = await Promise.all([
      api.get('/notifications'),
      api.get('/notifications?archived=true'),
    ]);
    notifications.value = activeResponse.data;
    archivedNotifications.value = archivedResponse.data;
  }

  async function setNotificationArchived(notificationId, archived) {
    await api.patch(`/notifications/${notificationId}/archive`, { archived });
    if (proactiveNotice.value?.id === notificationId) proactiveNotice.value = null;
    await refreshNotifications();
  }

  async function archiveReadNotifications() {
    const response = await api.post('/notifications/archive-read');
    await refreshNotifications();
    return response.data.archived;
  }

  async function undoOperation(operationId) {
    await api.post(`/operations/${operationId}/undo`);
    await refreshAfterRun();
  }

  async function refreshAfterRun() {
    const knownNotificationIds = new Set(notifications.value.map((item) => item.id));
    const [profileRes, plansRes, archivedPlansRes, dashboardRes, memoriesRes, notificationsRes, archivedNotificationsRes, operationsRes, runsRes, sessionsRes, archivedSessionsRes, emailRes, queueRes] = await Promise.all([
      api.get('/profile'),
      api.get('/plans'),
      api.get('/plans?archived=true'),
      api.get('/dashboard'),
      api.get('/memories'),
      api.get('/notifications'),
      api.get('/notifications?archived=true'),
      api.get('/operations'),
      api.get('/agent/runs'),
      api.get('/agent/sessions'),
      api.get('/agent/sessions?archived=true'),
      api.get('/settings/email'),
      api.get('/agent/queue'),
    ]);
    profile.value = profileRes.data;
    plans.value = plansRes.data;
    archivedPlans.value = archivedPlansRes.data;
    dashboard.value = dashboardRes.data;
    memories.value = memoriesRes.data;
    notifications.value = notificationsRes.data;
    archivedNotifications.value = archivedNotificationsRes.data;
    notifications.value
      .filter((item) => item.channel === 'browser' && item.status === 'sent' && !knownNotificationIds.has(item.id))
      .forEach((item) => showBrowserNotification(item.title, item.body));
    operations.value = operationsRes.data;
    runs.value = runsRes.data;
    sessions.value = sessionsRes.data;
    archivedSessions.value = archivedSessionsRes.data;
    emailConfiguration.value = emailRes.data;
    queuedMessages.value = queueRes.data || [];
    await refreshCurrentPlan();
    await loadConversation(activeSessionId.value);
    await refreshProactiveState();
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
    planningState.value = { intake: null, proposal: null };
    streamingRunId.value = null;
    streamingText.value = '';
    streamingReasoning.value = '';
    runEvents.value = [];
    closeEventSource();
  }

  async function startNewConversation() {
    resetConversationState();
    focusPlanId.value = null;
    activeView.value = 'home';
    await loadQueue();
  }

  async function enableBrowserNotifications() {
    if (!('Notification' in window)) return false;
    if (Notification.permission === 'default') {
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') return false;
    }
    if (Notification.permission !== 'granted') return false;
    if (!('serviceWorker' in navigator)) return true;
    try {
      const registration = await navigator.serviceWorker.ready;
      if (appSettings.value?.vapid_public_key && registration.pushManager) {
        const subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(appSettings.value.vapid_public_key),
        });
        const p256dh = subscription.getKey('p256dh');
        const auth = subscription.getKey('auth');
        if (p256dh && auth) {
          await api.post('/notifications/subscriptions', {
            endpoint: subscription.endpoint,
            keys: {
              p256dh: btoa(String.fromCharCode(...new Uint8Array(p256dh))),
              auth: btoa(String.fromCharCode(...new Uint8Array(auth))),
            },
          });
        }
      }
    } catch {
      // Page-level notifications still work without push.
    }
    return true;
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
    archivedNotifications,
    operations,
    runs,
    sessions,
    archivedSessions,
    emailConfiguration,
    appSettings,
    emailTestResult,
    schedulerStatus,
    proactiveNotice,
    currentRun,
    focusPlanId,
    traceOpen,
    activeSessionId,
    conversationMessages,
    planningState,
    queuedMessages,
    followUpBehavior,
    streamingText,
    streamingReasoning,
    streamingRunId,
    runEvents,
    loading,
    error,
    activeSubagents,
    planForRun,
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
    editMessage,
    submitPlanningAnswers,
    decidePlanProposal,
    continueInPlan,
    setPlanArchived,
    loadQueue,
    enqueueMessage,
    updateQueuedMessage,
    deleteQueuedMessage,
    moveQueuedMessage,
    sendQueuedMessage,
    steerRun,
    setFollowUpBehavior,
    setProactivePaused,
    testEmail,
    updateEmailSettings,
    deleteEmailCredentials,
    updateModelSettings,
    updateNotificationPolicy,
    refreshProactiveState,
    startProactiveSync,
    stopProactiveSync,
    dismissProactiveNotice,
    cancelCurrentRun,
    decideRunApproval,
    fetchChildRunEvents,
    confirmMemory,
    deleteMemory,
    markNotificationRead,
    setNotificationArchived,
    archiveReadNotifications,
    undoOperation,
    startNewConversation,
    enableBrowserNotifications,
  };
});
