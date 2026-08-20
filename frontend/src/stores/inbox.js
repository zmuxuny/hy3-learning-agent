import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client.js';
import { createLoadFence, errorMessage, removeById, replaceById, sameId } from './helpers.js';

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const output = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) output[index] = raw.charCodeAt(index);
  return output;
}

async function showBrowserNotification(title, body, interventionId = null) {
  const url = interventionId ? `/inbox/${interventionId}` : '/inbox';
  if ('serviceWorker' in navigator) {
    try {
      const registration = await navigator.serviceWorker.ready;
      await registration.showNotification(title, { body, data: { url } });
      return;
    } catch {
      // Fall back to the page-level Notification API.
    }
  }
  if ('Notification' in window && Notification.permission === 'granted') new Notification(title, { body });
}

export const useInboxStore = defineStore('inbox', () => {
  const notifications = ref([]);
  const archivedNotifications = ref([]);
  const replyTargetNotification = ref(null);
  const proactiveNotice = ref(null);
  const loading = ref({});
  const errors = ref({});
  const activeFence = createLoadFence();
  const archiveFence = createLoadFence();
  let proactiveTimer = null;

  const unreadCount = computed(() => notifications.value.filter((item) => !item.read_at).length);
  const allNotifications = computed(() => [...notifications.value, ...archivedNotifications.value]);

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

  const loadNotifications = () => loadCollection('active', '/notifications', notifications, activeFence);
  const loadArchivedNotifications = () => loadCollection('archived', '/notifications?archived=true', archivedNotifications, archiveFence);

  async function refreshNotifications() {
    return Promise.allSettled([loadNotifications(), loadArchivedNotifications()]);
  }

  async function markNotificationRead(notificationId) {
    const response = await api.post(`/notifications/${notificationId}/read`);
    notifications.value = replaceById(notifications.value, response.data);
    archivedNotifications.value = replaceById(archivedNotifications.value, response.data);
    return response.data;
  }

  async function openNotification(notificationId) {
    const response = await api.post(`/notifications/${notificationId}/open`);
    const item = response.data.notification;
    if (item) {
      notifications.value = replaceById(notifications.value, item);
      archivedNotifications.value = archivedNotifications.value.map((existing) => (
        sameId(existing.id, item.id) ? item : existing
      ));
      replyTargetNotification.value = {
        ...item,
        intervention_id: response.data.intervention_id,
      };
    }
    proactiveNotice.value = null;
    return response.data;
  }

  async function setNotificationArchived(notificationId, archived) {
    const response = await api.patch(`/notifications/${notificationId}/archive`, { archived });
    const item = response.data;
    if (archived) {
      notifications.value = removeById(notifications.value, notificationId);
      archivedNotifications.value = replaceById(archivedNotifications.value, item);
    } else {
      archivedNotifications.value = removeById(archivedNotifications.value, notificationId);
      notifications.value = replaceById(notifications.value, item);
    }
    if (sameId(proactiveNotice.value?.id, notificationId)) proactiveNotice.value = null;
    void refreshNotifications();
    return item;
  }

  async function archiveReadNotifications() {
    const response = await api.post('/notifications/archive-read');
    void refreshNotifications();
    return response.data.archived;
  }

  async function refreshProactiveState(loadSchedulerStatus) {
    try {
      const knownIds = new Set(notifications.value.map((item) => item.id));
      const itemsRequest = loadNotifications();
      void loadSchedulerStatus?.().catch(() => null);
      const items = await itemsRequest;
      const fresh = items.filter((item) => !knownIds.has(item.id) && item.channel === 'in_app' && item.status === 'sent');
      if (fresh.length) {
        proactiveNotice.value = fresh[0];
        void showBrowserNotification(
          fresh[0].title,
          fresh[0].body,
          fresh[0].intervention_id,
        );
      }
    } catch {
      // Background visibility never interrupts the active workspace.
    }
  }

  function startProactiveSync(loadSchedulerStatus) {
    if (proactiveTimer) return;
    proactiveTimer = window.setInterval(() => refreshProactiveState(loadSchedulerStatus), 15000);
  }

  function stopProactiveSync() {
    if (proactiveTimer) window.clearInterval(proactiveTimer);
    proactiveTimer = null;
  }

  function dismissProactiveNotice() { proactiveNotice.value = null; }

  async function enableBrowserNotifications(vapidPublicKey = null) {
    if (!('Notification' in window)) return false;
    if (Notification.permission === 'default' && await Notification.requestPermission() !== 'granted') return false;
    if (Notification.permission !== 'granted' || !('serviceWorker' in navigator)) return Notification.permission === 'granted';
    try {
      const registration = await navigator.serviceWorker.ready;
      if (vapidPublicKey && registration.pushManager) {
        const subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
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
      // Page-level notifications remain available without push.
    }
    return true;
  }

  return {
    notifications,
    archivedNotifications,
    replyTargetNotification,
    proactiveNotice,
    loading,
    errors,
    unreadCount,
    allNotifications,
    loadNotifications,
    loadArchivedNotifications,
    refreshNotifications,
    markNotificationRead,
    openNotification,
    setNotificationArchived,
    archiveReadNotifications,
    refreshProactiveState,
    startProactiveSync,
    stopProactiveSync,
    dismissProactiveNotice,
    enableBrowserNotifications,
  };
});
