import { api } from '$lib/services/api.svelte';
import type {
  Notification,
  NotificationChannelType,
  NotificationDestination,
  NotificationDestinationCreate,
  NotificationDestinationTestResult,
  NotificationDestinationUpdate,
  NotificationPreferences,
  NotificationProfile,
  NotificationProfileCreate,
  NotificationProfileUpdate
} from '$lib/types';

function createNotificationStore() {
  let notifications = $state<Notification[]>([]);
  let unreadCount = $state(0);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let lastFetch = $state<Date | null>(null);

  // Config state for destinations/profiles/preferences panel
  let channelTypes = $state<NotificationChannelType[]>([]);
  let destinations = $state<NotificationDestination[]>([]);
  let profiles = $state<NotificationProfile[]>([]);
  let preferences = $state<NotificationPreferences | null>(null);
  let configLoading = $state(false);
  let configError = $state<string | null>(null);

  // Polling state
  let pollInterval: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;
  const POLL_INTERVAL_MS = 60000; // 60 seconds

  function isHidden(): boolean {
    return typeof document !== 'undefined' && document.visibilityState === 'hidden';
  }

  // -- bell feed ----------------------------------------------------------

  async function fetch(): Promise<void> {
    loading = true;
    error = null;

    try {
      const response = await api.getNotifications();
      notifications = response.notifications;
      unreadCount = response.unreadCount;
      lastFetch = new Date();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to fetch notifications';
      console.error('Failed to fetch notifications:', e);
    } finally {
      loading = false;
    }
  }

  async function markRead(notificationId: string): Promise<void> {
    try {
      await api.markNotificationRead(notificationId);
      // Update local state
      notifications = notifications.map((n) =>
        n.id === notificationId ? { ...n, read: true } : n
      );
      unreadCount = Math.max(0, unreadCount - 1);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to mark notification as read';
      console.error('Failed to mark notification as read:', e);
    }
  }

  async function markAllRead(): Promise<void> {
    try {
      await api.markAllNotificationsRead();
      // Update local state
      notifications = notifications.map((n) => ({ ...n, read: true }));
      unreadCount = 0;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to mark all notifications as read';
      console.error('Failed to mark all notifications as read:', e);
    }
  }

  async function deleteNotification(notificationId: string): Promise<void> {
    try {
      await api.deleteNotification(notificationId);
      const wasUnread = notifications.find((n) => n.id === notificationId && !n.read);
      notifications = notifications.filter((n) => n.id !== notificationId);
      if (wasUnread) unreadCount = Math.max(0, unreadCount - 1);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to delete notification';
      console.error('Failed to delete notification:', e);
    }
  }

  async function clearAllNotifications(): Promise<void> {
    try {
      await api.clearAllNotifications();
      notifications = [];
      unreadCount = 0;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to clear notifications';
      console.error('Failed to clear notifications:', e);
    }
  }

  function startPolling(): void {
    if (pollInterval) return; // Already polling

    // Initial fetch
    fetch();

    // Skip scheduled fetches while tab is hidden.
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
    channelTypes = [];
    destinations = [];
    profiles = [];
    preferences = null;
    configLoading = false;
    configError = null;
  }

  // Trigger refresh when a task with notification completes
  function onTaskCompletedWithNotification(): void {
    // Immediate refresh after a short delay to allow backend to process
    setTimeout(() => {
      fetch();
    }, 500);
  }

  // -- config management (destinations/profiles/preferences) ---------------

  async function loadConfig(): Promise<void> {
    configLoading = true;
    configError = null;
    try {
      const [ct, dests, profs, prefs] = await Promise.all([
        api.getNotificationChannelTypes(),
        api.listNotificationDestinations(),
        api.listNotificationProfiles(),
        api.getNotificationPreferences()
      ]);
      channelTypes = ct;
      destinations = dests;
      profiles = profs;
      preferences = prefs;
    } catch (e) {
      configError = e instanceof Error ? e.message : 'Failed to load notification config';
      console.error('Failed to load notification config:', e);
    } finally {
      configLoading = false;
    }
  }

  async function createDestination(req: NotificationDestinationCreate): Promise<void> {
    const dest = await api.createNotificationDestination(req);
    destinations = [...destinations, dest];
  }

  async function updateDestination(
    destId: string,
    req: NotificationDestinationUpdate
  ): Promise<void> {
    const updated = await api.updateNotificationDestination(destId, req);
    destinations = destinations.map((d) => (d.id === destId ? updated : d));
  }

  async function deleteDestination(destId: string): Promise<void> {
    await api.deleteNotificationDestination(destId);
    const removed = destinations.find((d) => d.id === destId);
    destinations = destinations.filter((d) => d.id !== destId);
    // Cascade removal from local profile state
    if (removed) {
      profiles = profiles.map((p) => ({
        ...p,
        destinationNames: p.destinationNames.filter((n) => n !== removed.name)
      }));
    }
  }

  async function testDestination(
    destId: string,
    message?: string
  ): Promise<NotificationDestinationTestResult> {
    return api.testNotificationDestination(destId, message);
  }

  async function createProfile(req: NotificationProfileCreate): Promise<void> {
    const profile = await api.createNotificationProfile(req);
    profiles = [...profiles, profile];
  }

  async function updateProfile(
    profileId: string,
    req: NotificationProfileUpdate
  ): Promise<void> {
    const updated = await api.updateNotificationProfile(profileId, req);
    profiles = profiles.map((p) => (p.id === profileId ? updated : p));
  }

  async function deleteProfile(profileId: string): Promise<void> {
    await api.deleteNotificationProfile(profileId);
    profiles = profiles.filter((p) => p.id !== profileId);
  }

  async function updatePreferences(
    req: Partial<NotificationPreferences>
  ): Promise<void> {
    const updated = await api.updateNotificationPreferences(req);
    preferences = updated;
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
    // Config state
    get channelTypes() {
      return channelTypes;
    },
    get destinations() {
      return destinations;
    },
    get profiles() {
      return profiles;
    },
    get preferences() {
      return preferences;
    },
    get configLoading() {
      return configLoading;
    },
    get configError() {
      return configError;
    },
    // Bell feed actions
    fetch,
    markRead,
    markAllRead,
    deleteNotification,
    clearAllNotifications,
    startPolling,
    stopPolling,
    clear,
    onTaskCompletedWithNotification,
    // Config actions
    loadConfig,
    createDestination,
    updateDestination,
    deleteDestination,
    testDestination,
    createProfile,
    updateProfile,
    deleteProfile,
    updatePreferences
  };
}

export const notificationStore = createNotificationStore();
