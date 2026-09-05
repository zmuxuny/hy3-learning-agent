import { ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client.js';
import router from '../router.js';
import { isRunBlocking, isRunStreamable } from '../runState.js';
import { errorMessage, sameId } from './helpers.js';
import { useInboxStore } from './inbox.js';
import { useMemoryStore } from './memory.js';
import { usePlanStore } from './plan.js';
import { useRunStore } from './run.js';
import { useSessionStore } from './session.js';
import { useSettingsStore } from './settings.js';

const CORE_ROUTE_NAMES = new Set(['home', 'session']);

export const useShellStore = defineStore('shell', () => {
  const coreLoading = ref(false);
  const coreReady = ref(false);
  const error = ref('');
  const traceOpen = ref(false);
  const highlightedMessageId = ref(null);
  let bootstrapGeneration = 0;
  let selectionGeneration = 0;
  let hydratedPath = '';

  function domains() {
    return {
      inbox: useInboxStore(),
      memory: useMemoryStore(),
      plan: usePlanStore(),
      run: useRunStore(),
      session: useSessionStore(),
      settings: useSettingsStore(),
    };
  }

  function setError(requestError) {
    error.value = requestError ? errorMessage(requestError) : '';
  }

  async function bootstrap() {
    const generation = ++bootstrapGeneration;
    const { inbox, memory, plan, run, session, settings } = domains();
    coreLoading.value = true;
    error.value = '';

    void Promise.allSettled([
      plan.loadArchivedPlans(),
      session.loadArchivedSessions(),
      settings.loadOptional(),
      memory.loadMemories(),
      inbox.loadNotifications(),
      inbox.loadArchivedNotifications(),
      run.loadOperations(),
    ]);

    const results = await Promise.allSettled([
      settings.loadProfile(),
      settings.loadOnboardingStatus(),
      plan.loadActivePlans(),
      session.loadActiveSessions(),
      run.loadRuns(),
      session.loadQueue(),
    ]);
    if (generation !== bootstrapGeneration) return;
    coreReady.value = true;
    coreLoading.value = false;
    const failed = results.find((result) => result.status === 'rejected');
    if (failed && !settings.profile && !plan.plans.length && !session.sessions.length) setError(failed.reason);
    if (settings.onboardingStatus?.requires_onboarding && router.currentRoute.value.name !== 'onboarding') {
      await router.replace({ name: 'onboarding' });
    } else if (!settings.onboardingStatus?.requires_onboarding && router.currentRoute.value.name === 'onboarding') {
      await router.replace({ name: 'home' });
    }
    await hydrateRoute(router.currentRoute.value, { force: true });
  }

  async function hydrateRoute(route, { force = false } = {}) {
    if (!coreReady.value) return;
    if (!force && hydratedPath === route.fullPath) return;
    hydratedPath = route.fullPath;
    const { inbox, plan, session } = domains();
    if (route.name === 'session') {
      let target = [...session.sessions, ...session.archivedSessions].find(
        (item) => sameId(item.id, route.params.sessionId),
      );
      if (!target) {
        await session.loadArchivedSessions().catch(() => null);
        target = [...session.sessions, ...session.archivedSessions].find(
          (item) => sameId(item.id, route.params.sessionId),
        );
      }
      if (!target) {
        await router.replace({ name: 'home' });
        return;
      }
      await selectSession(target, { navigate: false });
      return;
    }
    if (route.name === 'plan') {
      let target = [...plan.plans, ...plan.archivedPlans].find((item) => sameId(item.id, route.params.planId));
      if (!target) {
        await plan.loadArchivedPlans().catch(() => null);
        target = [...plan.plans, ...plan.archivedPlans].find((item) => sameId(item.id, route.params.planId));
      }
      if (!target) {
        plan.clearDetail();
        await router.replace({ name: 'plans' });
        return;
      }
      await plan.loadPlan(target.id);
      plan.setFocus(target.id);
      return;
    }
    if (route.name === 'inbox-intervention') {
      if (inbox.replyTargetNotification?.intervention_id === route.params.interventionId) return;
      let target = inbox.allNotifications.find((item) => item.intervention_id === route.params.interventionId);
      if (!target) {
        await Promise.allSettled([inbox.loadNotifications(), inbox.loadArchivedNotifications()]);
        target = inbox.allNotifications.find((item) => item.intervention_id === route.params.interventionId);
      }
      if (target) await openNotification(target, { navigate: false });
      else await router.replace({ name: 'inbox' });
      return;
    }
    if (route.name === 'home') {
      // The route owns the view, while the selected conversation remains a separate execution context.
      return;
    }
    if (route.name === 'plans' || route.name === 'archives') plan.clearDetail();
  }

  async function selectPlan(planId) {
    const { plan } = domains();
    const target = [...plan.plans, ...plan.archivedPlans].find((item) => sameId(item.id, planId));
    if (!target) return false;
    await router.push({ name: 'plan', params: { planId: target.id } });
    return true;
  }

  async function selectSession(target, { navigate = true } = {}) {
    if (navigate) {
      const selected = await selectSession(target, { navigate: false });
      if (!selected) return false;
      const location = { name: 'session', params: { sessionId: target.id } };
      hydratedPath = router.resolve(location).fullPath;
      await router.push(location);
      return true;
    }
    const generation = ++selectionGeneration;
    const { inbox, plan, run, session } = domains();
    run.closeEventSource();
    inbox.replyTargetNotification = null;
    session.activeSessionId = target.id;
    plan.setFocus(target.plan_id);
    const [messagesResult, queueResult] = await Promise.allSettled([
      session.loadConversation(target.id),
      session.loadQueue(),
    ]);
    if (messagesResult.status === 'rejected') setError(messagesResult.reason);
    if (queueResult.status === 'rejected') setError(queueResult.reason);
    if (generation !== selectionGeneration) return false;
    let selectedRun = run.runs.find((item) => item.id === target.last_run_id);
    if (!selectedRun && target.last_run_id) selectedRun = (await api.get(`/agent/runs/${target.last_run_id}`)).data;
    if (generation !== selectionGeneration) return false;
    if (!selectedRun) {
      run.resetCurrentRun();
      return true;
    }
    await run.inspectRun(selectedRun);
    return true;
  }

  function resetExecutionContext() {
    selectionGeneration += 1;
    const { inbox, plan, run, session } = domains();
    session.resetConversation();
    run.resetCurrentRun();
    plan.setFocus(null);
    inbox.replyTargetNotification = null;
  }

  async function startNewConversation() {
    resetExecutionContext();
    const location = { name: 'home' };
    hydratedPath = router.resolve(location).fullPath;
    await router.push(location);
    await domains().session.loadQueue().catch(setError);
  }

  async function startRun(objective, planId = undefined, options = {}) {
    if (!objective.trim()) return false;
    const { inbox, plan, run, session } = domains();
    const resolvedPlanId = planId === undefined ? plan.focusPlanId : planId;
    const activeSession = session.sessions.find((item) => sameId(item.id, session.activeSessionId));
    if (resolvedPlanId != null && (!activeSession || !sameId(activeSession.plan_id, resolvedPlanId))) {
      try {
        const prior = session.sessions.find((item) => sameId(item.plan_id, resolvedPlanId));
        if (prior) await selectSession(prior);
        else if (activeSession?.plan_id == null && activeSession?.linked_plan_ids?.some((id) => sameId(id, resolvedPlanId))) {
          await continueInPlan(resolvedPlanId);
        } else {
          resetExecutionContext();
          plan.setFocus(resolvedPlanId);
        }
      } catch (requestError) {
        setError(requestError);
        return false;
      }
    }
    if (options.mode === 'queue' && run.currentRun && isRunBlocking(run.currentRun.status)) {
      await session.enqueueMessage(objective, resolvedPlanId ?? null);
      return 'queued';
    }
    if (options.mode === 'interrupt' && run.currentRun && isRunBlocking(run.currentRun.status)) {
      await api.post(`/agent/runs/${run.currentRun.id}/cancel`);
      const deadline = Date.now() + 8000;
      while (Date.now() < deadline) {
        const polled = (await api.get(`/agent/runs/${run.currentRun.id}`)).data;
        run.currentRun = polled;
        if (!isRunBlocking(polled.status)) break;
        await new Promise((resolve) => window.setTimeout(resolve, 300));
      }
    }
    run.closeEventSource();
    run.runEvents = [];
    error.value = '';
    try {
      const target = inbox.replyTargetNotification;
      const response = await api.post('/agent/runs', {
        objective,
        plan_id: resolvedPlanId,
        session_id: session.activeSessionId,
        reply_to_intervention_id: target?.intervention_id ?? null,
        trigger: 'user_message',
      });
      run.prependRun(response.data);
      plan.setFocus(response.data.plan_id ?? resolvedPlanId);
      session.activeSessionId = response.data.session_id;
      if (!session.conversationMessages.some((message) => message.run_id === response.data.id && message.role === 'user')) {
        session.conversationMessages.push({
          id: `pending-${response.data.id}`,
          session_id: response.data.session_id,
          run_id: response.data.id,
          role: 'user',
          content: objective,
          reply_to_intervention_id: target?.intervention_id ?? null,
          message_metadata: {},
          created_at: new Date().toISOString(),
          pending: true,
        });
      }
      inbox.replyTargetNotification = null;
      await Promise.allSettled([session.loadActiveSessions(), session.loadQueue()]);
      const location = { name: 'session', params: { sessionId: response.data.session_id } };
      hydratedPath = router.resolve(location).fullPath;
      await router.push(location);
      run.subscribeToRun(response.data.id);
      return true;
    } catch (requestError) {
      setError(requestError);
      return false;
    }
  }

  async function enqueueMessage(objective) {
    const { inbox, plan, session } = domains();
    const response = await api.post('/agent/queue', {
      objective,
      session_id: session.activeSessionId || null,
      plan_id: plan.focusPlanId ?? null,
      reply_to_intervention_id: inbox.replyTargetNotification?.intervention_id ?? null,
    });
    inbox.replyTargetNotification = null;
    await session.loadQueue();
    return response.data;
  }

  async function sendQueuedMessage(messageId) {
    const { plan, run, session } = domains();
    const current = session.queuedMessages.find((item) => sameId(item.id, messageId));
    const response = await api.post(`/agent/queue/${messageId}/send`, { expected_version: current?.version ?? 1 });
    run.prependRun(response.data);
    plan.setFocus(response.data.plan_id ?? plan.focusPlanId);
    session.activeSessionId = response.data.session_id || session.activeSessionId;
    await Promise.allSettled([session.loadActiveSessions(), session.loadQueue()]);
    const location = { name: 'session', params: { sessionId: session.activeSessionId } };
    hydratedPath = router.resolve(location).fullPath;
    await router.push(location);
    run.subscribeToRun(response.data.id);
    return true;
  }

  async function setPlanArchived(planId, archived) {
    try {
      const { plan, session } = domains();
      const result = await plan.setArchived(planId, archived);
      if (result.detailCleared && router.currentRoute.value.name === 'plan') await router.replace({ name: 'plans' });
      if (result.focusCleared && !session.activeSessionId && CORE_ROUTE_NAMES.has(router.currentRoute.value.name)) {
        plan.setFocus(null);
      }
      return true;
    } catch (requestError) {
      setError(requestError);
      return false;
    }
  }

  async function setSessionArchived(sessionId, archived) {
    try {
      const { session } = domains();
      await session.setArchived(sessionId, archived);
      if (archived && sameId(session.activeSessionId, sessionId)) await startNewConversation();
      return true;
    } catch (requestError) {
      setError(requestError);
      return false;
    }
  }

  async function inspectRun(target) {
    const { plan, run, session } = domains();
    const targetSession = [...session.sessions, ...session.archivedSessions].find(
      (item) => sameId(item.id, target.session_id),
    );
    if (targetSession) await selectSession(targetSession);
    else {
      session.activeSessionId = target.session_id || null;
      plan.setFocus(target.plan_id);
      await Promise.allSettled([session.loadConversation(session.activeSessionId), session.loadQueue()]);
      const location = { name: 'home' };
      hydratedPath = router.resolve(location).fullPath;
      await router.push(location);
    }
    await run.inspectRun(target);
  }

  async function inspectChildRun(runId) {
    const target = (await api.get(`/agent/runs/${runId}`)).data;
    await inspectRun(target);
  }

  async function editMessage(messageId, content) {
    const { plan, run, session } = domains();
    run.closeEventSource();
    try {
      const response = await api.post(`/agent/messages/${messageId}/edit`, { content, rerun: true });
      run.prependRun(response.data);
      session.activeSessionId = response.data.session_id;
      plan.setFocus(response.data.plan_id);
      await Promise.allSettled([session.loadConversation(session.activeSessionId), session.loadActiveSessions()]);
      run.subscribeToRun(response.data.id);
      return true;
    } catch (requestError) {
      setError(requestError);
      return false;
    }
  }

  async function submitPlanningAnswers(answers) {
    const { run, session } = domains();
    if (!session.activeSessionId || !answers.length) return false;
    const questions = session.planningState.intake?.open_questions || [];
    const receiptAnswers = answers.map((answer) => ({
      ...answer,
      prompt: questions.find((question) => question.id === answer.question_id)?.prompt,
    }));
    try {
      const response = await api.post(`/agent/sessions/${session.activeSessionId}/planning/answers`, { answers });
      run.prependRun(response.data);
      session.conversationMessages.push({
        id: `planning-answers-${response.data.id}`,
        session_id: session.activeSessionId,
        run_id: response.data.id,
        role: 'user',
        content: '',
        message_metadata: { ui_kind: 'planning_answers', answer_count: answers.length, answers: receiptAnswers },
        created_at: new Date().toISOString(),
        pending: true,
      });
      await session.loadActiveSessions().catch(setError);
      run.subscribeToRun(response.data.id);
      return true;
    } catch (requestError) {
      setError(requestError);
      return false;
    }
  }

  async function decidePlanProposal(proposalId, accepted) {
    const { plan, run, session } = domains();
    try {
      const response = await api.post(`/agent/plan-proposals/${proposalId}/decision`, { accepted });
      session.setPlanningState({ ...session.planningState, proposal: response.data });
      await Promise.allSettled([
        plan.loadActivePlans(), plan.loadArchivedPlans(), run.loadOperations(), run.loadRuns(), session.loadActiveSessions(),
      ]);
      return true;
    } catch (requestError) {
      setError(requestError);
      return false;
    }
  }

  async function continueInPlan(planId) {
    const { session } = domains();
    if (!session.activeSessionId) return false;
    const response = await api.post(`/agent/sessions/${session.activeSessionId}/handoff`, { plan_id: planId });
    await session.loadActiveSessions();
    const target = session.sessions.find((item) => sameId(item.id, response.data.id)) || response.data;
    await selectSession(target);
    return true;
  }

  async function openNotification(notificationOrId, { navigate = true } = {}) {
    const { inbox, session } = domains();
    const item = typeof notificationOrId === 'object'
      ? notificationOrId
      : inbox.allNotifications.find((candidate) => sameId(candidate.id, notificationOrId));
    if (!item) return false;
    try {
      const opened = await inbox.openNotification(item.id);
      await Promise.allSettled([session.loadActiveSessions(), session.loadArchivedSessions()]);
      const targetSession = [...session.sessions, ...session.archivedSessions].find(
        (candidate) => sameId(candidate.id, opened.session_id),
      );
      if (!targetSession) throw new Error('提醒所属对话暂时不可用');
      await selectSession(targetSession, { navigate: false });
      inbox.replyTargetNotification = {
        ...opened.notification,
        intervention_id: opened.intervention_id,
      };
      highlightedMessageId.value = opened.message_id;
      if (navigate) {
        const interventionId = opened.intervention_id ?? opened.notification?.intervention_id;
        if (interventionId) await router.push({ name: 'inbox-intervention', params: { interventionId } });
      }
      return true;
    } catch (requestError) {
      setError(requestError);
      return false;
    }
  }

  async function triggerHeartbeat() {
    const { inbox, run, settings } = domains();
    try {
      const response = await api.post('/agent/heartbeat');
      run.prependRun(response.data);
      settings.schedulerStatus = { ...(settings.schedulerStatus || {}), active: true, latest_run_id: response.data.id, latest_run_status: response.data.status };
      void inbox.refreshProactiveState(settings.loadSchedulerStatus);
      return response.data;
    } catch (requestError) {
      setError(requestError);
      return null;
    }
  }

  async function refreshAfterRun() {
    const { inbox, memory, plan, run, session, settings } = domains();
    await Promise.allSettled([
      settings.loadProfile(), plan.loadActivePlans(), session.loadActiveSessions(), run.loadRuns(), session.loadQueue(),
    ]);
    void Promise.allSettled([
      plan.loadArchivedPlans(), settings.loadDashboard(), settings.loadEmailConfiguration(), memory.loadMemories(),
      inbox.loadNotifications(), inbox.loadArchivedNotifications(), run.loadOperations(), session.loadArchivedSessions(),
    ]);
    if (run.currentRun) run.currentRun = run.runs.find((item) => item.id === run.currentRun.id) || run.currentRun;
    const blocking = session.activeSessionId
      ? run.runs.find((item) => sameId(item.session_id, session.activeSessionId) && isRunBlocking(item.status))
      : null;
    if (blocking && blocking.id !== run.currentRun?.id) {
      run.currentRun = blocking;
      run.runEvents = await run.loadRunEvents(blocking.id, true);
      if (isRunStreamable(blocking.status)) run.subscribeToRun(blocking.id, false);
    }
    await session.loadConversation(session.activeSessionId).catch(setError);
  }

  function configureRunEvents() {
    const { run, session } = domains();
    run.setEventHandlers({
      onTerminal: refreshAfterRun,
      onMessagesReconciled: (sessionId, messages) => {
        if (sameId(session.activeSessionId, sessionId)) session.conversationMessages = messages;
      },
      onToolCompleted: async (payload) => {
        const toolName = payload.payload?.name;
        const result = payload.payload?.result || {};
        if (result.ok && toolName === 'planning_intake_update' && result.data?.open_questions) {
          session.setPlanningState({ ...session.planningState, intake: { ...result.data, source_run_id: run.currentRun?.id } });
        } else if (result.ok && toolName === 'plan_proposal_create' && session.activeSessionId) {
          const response = await api.get(`/agent/sessions/${session.activeSessionId}/planning`);
          session.setPlanningState(response.data);
        }
      },
      onSteer: (payload, runId) => {
        const content = payload.payload?.content || '';
        const steerId = payload.payload?.steer_id || `sequence:${payload.sequence}`;
        if (!content || session.conversationMessages.some((message) => (
          message.run_id === runId && message.role === 'user'
          && message.message_metadata?.ui_kind === 'steer'
          && message.message_metadata?.steer_id === steerId
        ))) return;
        session.conversationMessages.push({
          id: `steer-${steerId}`,
          session_id: session.activeSessionId,
          run_id: runId,
          role: 'user',
          content,
          message_metadata: { ui_kind: 'steer', steer_id: steerId },
          created_at: new Date().toISOString(),
        });
      },
    });
  }

  return {
    coreLoading,
    coreReady,
    error,
    traceOpen,
    highlightedMessageId,
    setError,
    bootstrap,
    hydrateRoute,
    selectPlan,
    selectSession,
    startNewConversation,
    startRun,
    enqueueMessage,
    sendQueuedMessage,
    setPlanArchived,
    setSessionArchived,
    inspectRun,
    inspectChildRun,
    editMessage,
    submitPlanningAnswers,
    decidePlanProposal,
    continueInPlan,
    openNotification,
    triggerHeartbeat,
    refreshAfterRun,
    configureRunEvents,
  };
});
