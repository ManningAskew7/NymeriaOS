import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
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
  // Bumped by the reload hook: a response from the previous backend lands nowhere.
  let identityGeneration = 0;

  function isHidden(): boolean {
    return typeof document !== 'undefined' && document.visibilityState === 'hidden';
  }

  function current(generation: number): boolean {
    return generation === identityGeneration;
  }

  // Notifications and the destinations/profiles/preferences config are per
  // backend and per account. Drop them on every connection switch: the bell
  // list was only replaced by the next poll, and the config lists never were
  // (#242). Polling itself is the switch orchestrator's to stop and restart.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    loading = false;
    clear();
  });

  // -- bell feed ----------------------------------------------------------

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

  async function deleteNotification(notificationId: string): Promise<void> {
    const generation = identityGeneration;
    try {
      await api.deleteNotification(notificationId);
      if (!current(generation)) return;
      const wasUnread = notifications.find((n) => n.id === notificationId && !n.read);
      notifications = notifications.filter((n) => n.id !== notificationId);
      if (wasUnread) unreadCount = Math.max(0, unreadCount - 1);
    } catch (e) {
      if (!current(generation)) return;
      error = humanizeErrorText(e, { action: 'delete', resource: 'the notification' });
      console.error('Failed to delete notification:', e);
    }
  }

  async function clearAllNotifications(): Promise<void> {
    const generation = identityGeneration;
    try {
      await api.clearAllNotifications();
      if (!current(generation)) return;
      notifications = [];
      unreadCount = 0;
    } catch (e) {
      if (!current(generation)) return;
      error = humanizeErrorText(e, { action: 'delete', resource: 'your notifications' });
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
    const generation = identityGeneration;
    configLoading = true;
    configError = null;
    try {
      const [ct, dests, profs, prefs] = await Promise.all([
        api.getNotificationChannelTypes(),
        api.listNotificationDestinations(),
        api.listNotificationProfiles(),
        api.getNotificationPreferences()
      ]);
      if (!current(generation)) return;
      channelTypes = ct;
      destinations = dests;
      profiles = profs;
      preferences = prefs;
    } catch (e) {
      if (!current(generation)) return;
      configError = humanizeErrorText(e, { action: 'load', resource: 'the notification settings' });
      console.error('Failed to load notification config:', e);
    } finally {
      if (current(generation)) configLoading = false;
    }
  }

  async function createDestination(req: NotificationDestinationCreate): Promise<void> {
    const generation = identityGeneration;
    const dest = await api.createNotificationDestination(req);
    if (current(generation)) destinations = [...destinations, dest];
  }

  async function updateDestination(
    destId: string,
    req: NotificationDestinationUpdate
  ): Promise<void> {
    const generation = identityGeneration;
    const updated = await api.updateNotificationDestination(destId, req);
    if (current(generation)) destinations = destinations.map((d) => (d.id === destId ? updated : d));
  }

  async function deleteDestination(destId: string): Promise<void> {
    const generation = identityGeneration;
    await api.deleteNotificationDestination(destId);
    if (!current(generation)) return;
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
    const generation = identityGeneration;
    const profile = await api.createNotificationProfile(req);
    if (current(generation)) profiles = [...profiles, profile];
  }

  async function updateProfile(
    profileId: string,
    req: NotificationProfileUpdate
  ): Promise<void> {
    const generation = identityGeneration;
    const updated = await api.updateNotificationProfile(profileId, req);
    if (current(generation)) profiles = profiles.map((p) => (p.id === profileId ? updated : p));
  }

  async function deleteProfile(profileId: string): Promise<void> {
    const generation = identityGeneration;
    await api.deleteNotificationProfile(profileId);
    if (current(generation)) profiles = profiles.filter((p) => p.id !== profileId);
  }

  async function updatePreferences(
    req: Partial<NotificationPreferences>
  ): Promise<void> {
    const generation = identityGeneration;
    const updated = await api.updateNotificationPreferences(req);
    if (current(generation)) preferences = updated;
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
