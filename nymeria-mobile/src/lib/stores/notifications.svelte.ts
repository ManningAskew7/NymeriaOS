import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type { Notification } from '$lib/types';

function createNotificationStore() {
  let notifications = $state<Notification[]>([]);
  let unreadCount = $state(0);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let lastFetch = $state<Date | null>(null);

  // Polling state
  let pollInterval: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;
  const POLL_INTERVAL_MS = 60000; // 60 seconds
  // Bumped by the reload hook: a response from the previous backend lands nowhere.
  let identityGeneration = 0;

  function isHidden(): boolean {
    return typeof document !== 'undefined' && document.visibilityState === 'hidden';
  }

  function current(generation: number): boolean {
    return generation === identityGeneration;
  }

  // Notifications are per backend and per account: drop them on every
  // connection switch rather than waiting for the next poll (#242). Polling
  // itself is the switch orchestrator's to stop and restart.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    loading = false;
    clear();
  });

  async function fetch(): Promise<void> {
    const generation = identityGeneration;
    loading = true;
    error = null;

    try {
      const response = await api.getNotifications();
      if (!current(generation)) return;
      notifications = response.notifications;
      unreadCount = response.unreadCount;
      lastFetch = new Date();
    } catch (e) {
      if (!current(generation)) return;
      error = humanizeErrorText(e, { action: 'load', resource: 'your notifications' });
      console.error('Failed to fetch notifications:', e);
    } finally {
      if (current(generation)) loading = false;
    }
  }

  async function markRead(notificationId: string): Promise<void> {
    const generation = identityGeneration;
    try {
      await api.markNotificationRead(notificationId);
      if (!current(generation)) return;
      // Update local state
      notifications = notifications.map((n) =>
        n.id === notificationId ? { ...n, read: true } : n
      );
      unreadCount = Math.max(0, unreadCount - 1);
    } catch (e) {
      if (!current(generation)) return;
      error = humanizeErrorText(e, { action: 'update', resource: 'the notification' });
      console.error('Failed to mark notification as read:', e);
    }
  }

  async function markAllRead(): Promise<void> {
    const generation = identityGeneration;
    try {
      await api.markAllNotificationsRead();
      if (!current(generation)) return;
      // Update local state
      notifications = notifications.map((n) => ({ ...n, read: true }));
      unreadCount = 0;
    } catch (e) {
      if (!current(generation)) return;
      error = humanizeErrorText(e, { action: 'update', resource: 'your notifications' });
      console.error('Failed to mark all notifications as read:', e);
    }
  }

  function startPolling(): void {
    if (pollInterval) return; // Already polling

    // Initial fetch
    fetch();

    pollInterval = setInterval(() => {
      if (!isHidden()) fetch();
    }, POLL_INTERVAL_MS);

    if (typeof document !== 'undefined') {
      visibilityHandler = () => {
        if (!isHidden()) fetch();
      };
      document.addEventListener('visibilitychange', visibilityHandler);
    }
  }

  function stopPolling(): void {
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
    if (visibilityHandler && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', visibilityHandler);
      visibilityHandler = null;
    }
  }

  function clear(): void {
    notifications = [];
    unreadCount = 0;
    error = null;
    lastFetch = null;
  }

  // Trigger refresh when a task with notification completes
  function onTaskCompletedWithNotification(): void {
    // Immediate refresh after a short delay to allow backend to process
    setTimeout(() => {
      fetch();
    }, 500);
  }

  return {
    get notifications() {
      return notifications;
    },
    get unreadCount() {
      return unreadCount;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    get lastFetch() {
      return lastFetch;
    },
    fetch,
    markRead,
    markAllRead,
    startPolling,
    stopPolling,
    clear,
    onTaskCompletedWithNotification
  };
}

export const notificationStore = createNotificationStore();
