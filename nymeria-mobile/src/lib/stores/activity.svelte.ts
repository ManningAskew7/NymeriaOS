import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import type { ActivityEntry } from '$lib/types';

const POLL_INTERVAL = 45000; // 45 seconds

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

function createActivityStore() {
  let entries = $state<ActivityEntry[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let lastFetch = $state<Date | null>(null);
  let pollIntervalId: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;

  let currentThreadFilter = $state<string | undefined>(undefined);
  let currentPollThreadId = $state<string | undefined>(undefined);

  async function fetch(limit: number = 50, threadId?: string): Promise<void> {
    loading = true;
    error = null;
    currentThreadFilter = threadId;

    try {
      const response = await api.getActivity(limit, currentThreadFilter);
      entries = response.entries;
      lastFetch = new Date();
    } catch (e) {
      error = humanizeErrorText(e, { action: 'load', resource: 'the activity feed' });
      console.error('Failed to fetch activity:', e);
    } finally {
      loading = false;
    }
  }

  function startPolling(threadId?: string): void {
    // If already polling for a different threadId, restart
    if (pollIntervalId && currentPollThreadId !== threadId) {
      stopPolling();
    }
    if (pollIntervalId) return;

    currentPollThreadId = threadId;
    fetch(50, threadId);
    pollIntervalId = setInterval(() => {
      if (!isHidden()) fetch(50, currentPollThreadId);
    }, POLL_INTERVAL);

    if (typeof document !== 'undefined') {
      visibilityHandler = () => {
        if (!isHidden()) fetch(50, currentPollThreadId);
      };
      document.addEventListener('visibilitychange', visibilityHandler);
    }
  }

  function stopPolling(): void {
    if (pollIntervalId) {
      clearInterval(pollIntervalId);
      pollIntervalId = null;
    }
    if (visibilityHandler && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', visibilityHandler);
      visibilityHandler = null;
    }
    currentPollThreadId = undefined;
  }

  function clear(): void {
    entries = [];
    error = null;
    lastFetch = null;
  }

  return {
    get entries() { return entries; },
    get loading() { return loading; },
    get error() { return error; },
    get lastFetch() { return lastFetch; },
    get count() { return entries.length; },
    fetch,
    startPolling,
    stopPolling,
    clear
  };
}

export const activityStore = createActivityStore();
