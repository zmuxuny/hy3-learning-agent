import { ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client.js';
import { createLoadFence, errorMessage } from './helpers.js';

export const useSettingsStore = defineStore('settings', () => {
  const profile = ref(null);
  const dashboard = ref({ activity: [], achievements: [], due_review_count: 0, open_quiz_count: 0 });
  const emailConfiguration = ref(null);
  const appSettings = ref(null);
  const onboardingStatus = ref(null);
  const emailTestResult = ref(null);
  const schedulerStatus = ref(null);
  const followUpBehavior = ref('steer');
  const loading = ref({});
  const errors = ref({});
  let pendingSmtpDiagnosticActionId = null;
  const fences = new Map();

  function fenceFor(key) {
    if (!fences.has(key)) fences.set(key, createLoadFence());
    return fences.get(key);
  }

  async function load(key, path, assign) {
    const fence = fenceFor(key);
    const generation = fence.next();
    loading.value = { ...loading.value, [key]: true };
    try {
      const response = await api.get(path);
      if (fence.current(generation)) {
        assign(response.data);
        const nextErrors = { ...errors.value };
        delete nextErrors[key];
        errors.value = nextErrors;
      }
      return response.data;
    } catch (error) {
      if (fence.current(generation)) errors.value = { ...errors.value, [key]: errorMessage(error) };
      throw error;
    } finally {
      if (fence.current(generation)) loading.value = { ...loading.value, [key]: false };
    }
  }

  const loadProfile = () => load('profile', '/profile', (data) => { profile.value = data; });
  const loadDashboard = () => load('dashboard', '/dashboard', (data) => { dashboard.value = data; });
  const loadEmailConfiguration = () => load('email', '/settings/email', (data) => { emailConfiguration.value = data; });
  const loadAppSettings = () => load('app', '/settings', (data) => { appSettings.value = data; });
  const loadOnboardingStatus = () => load('onboarding', '/settings/onboarding', (data) => {
    onboardingStatus.value = data;
  });
  const loadSchedulerStatus = () => load('proactive', '/settings/proactive', (data) => { schedulerStatus.value = data; });
  const loadFollowUpBehavior = () => load('followup', '/settings/followup', (data) => {
    followUpBehavior.value = data?.follow_up_behavior || 'steer';
  });

  async function loadOptional() {
    return Promise.allSettled([
      loadDashboard(),
      loadEmailConfiguration(),
      loadAppSettings(),
      loadSchedulerStatus(),
      loadFollowUpBehavior(),
    ]);
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

  function createClientActionId() {
    if (!globalThis.crypto?.randomUUID) throw new Error('当前浏览器无法生成可靠的请求标识');
    return globalThis.crypto.randomUUID();
  }

  async function testEmail(channel, sendMessage = false) {
    emailTestResult.value = null;
    const queuesSmtpMessage = channel === 'smtp' && sendMessage;
    try {
      if (queuesSmtpMessage && !pendingSmtpDiagnosticActionId) pendingSmtpDiagnosticActionId = createClientActionId();
      const payload = { channel, send_message: sendMessage };
      if (queuesSmtpMessage) payload.action_id = pendingSmtpDiagnosticActionId;
      const response = await api.post('/settings/email/test', payload);
      if (queuesSmtpMessage) pendingSmtpDiagnosticActionId = null;
      emailTestResult.value = response.data;
      await loadEmailConfiguration().catch(() => null);
      return true;
    } catch (error) {
      const status = error.response?.status;
      if (queuesSmtpMessage && status >= 400 && status < 500) pendingSmtpDiagnosticActionId = null;
      emailTestResult.value = { ok: false, error: errorMessage(error) };
      return false;
    }
  }

  async function updateEmailSettings(payload) {
    const response = await api.put('/settings/email', payload);
    await loadEmailConfiguration();
    return response.data;
  }

  async function deleteEmailCredentials() {
    const response = await api.delete('/settings/email');
    await loadEmailConfiguration();
    return response.data;
  }

  async function updateModelSettings(payload) {
    const response = await api.put('/settings/model', payload);
    appSettings.value = { ...(appSettings.value || {}), ...response.data };
    onboardingStatus.value = {
      ...(onboardingStatus.value || {}),
      api_key_configured: Boolean(response.data.api_key_configured),
      requires_onboarding: !response.data.api_key_configured,
      model: response.data.model,
      base_url: response.data.base_url,
    };
    return response.data;
  }

  async function verifyModelConnection(payload) {
    const response = await api.post('/settings/model/test', payload);
    return response.data;
  }

  async function updateNotificationPolicy(payload) {
    const response = await api.put('/settings/notification', payload);
    await loadProfile().catch(() => null);
    return response.data;
  }

  return {
    profile,
    dashboard,
    emailConfiguration,
    appSettings,
    onboardingStatus,
    emailTestResult,
    schedulerStatus,
    followUpBehavior,
    loading,
    errors,
    loadProfile,
    loadDashboard,
    loadEmailConfiguration,
    loadAppSettings,
    loadOnboardingStatus,
    loadSchedulerStatus,
    loadFollowUpBehavior,
    loadOptional,
    setFollowUpBehavior,
    setProactivePaused,
    testEmail,
    updateEmailSettings,
    deleteEmailCredentials,
    updateModelSettings,
    verifyModelConnection,
    updateNotificationPolicy,
  };
});
