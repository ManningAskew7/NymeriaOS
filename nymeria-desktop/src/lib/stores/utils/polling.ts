/**
 * Reusable polling store factory for Svelte 5 runes.
 *
 * Creates a store that periodically fetches data from an API endpoint.
 * Provides loading, error, and lastFetch state management.
 */

export interface PollingStoreOptions<T> {
  /** Function that fetches data. Should return a promise with the data. */
  fetchFn: () => Promise<T>;
  /** Polling interval in milliseconds (default: 30000) */
  pollInterval?: number;
  /** Function to extract items array from response (if applicable) */
  extractItems?: (response: T) => unknown[];
  /** Name for error messages */
  errorName?: string;
}

export interface PollingStoreState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  lastFetch: Date | null;
}

export interface PollingStore<T> {
  readonly data: T | null;
  readonly loading: boolean;
  readonly error: string | null;
  readonly lastFetch: Date | null;
  readonly count: number;
  fetch(): Promise<void>;
  startPolling(): void;
  stopPolling(): void;
  clear(): void;
}

/**
 * Create a polling store with Svelte 5 runes.
 *
 * @example
 * ```ts
 * const activityStore = createPollingStore({
 *   fetchFn: () => api.getActivity(),
 *   extractItems: (r) => r.entries,
 *   errorName: 'activity'
 * });
 * ```
 */
export function createPollingStore<T>(
  options: PollingStoreOptions<T>
): PollingStore<T> {
  const {
    fetchFn,
    pollInterval = 30000,
    extractItems,
    errorName = 'data'
  } = options;

  let data = $state<T | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let lastFetch = $state<Date | null>(null);
  let pollIntervalId: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;

  const count = $derived(() => {
    if (data && extractItems) {
      return extractItems(data).length;
    }
    return 0;
  });

  async function fetch(): Promise<void> {
    loading = true;
    error = null;

    try {
      data = await fetchFn();
      lastFetch = new Date();
    } catch (e) {
      error = e instanceof Error ? e.message : `Failed to fetch ${errorName}`;
      console.error(`Failed to fetch ${errorName}:`, e);
    } finally {
      loading = false;
    }
  }

  function isHidden(): boolean {
    return typeof document !== 'undefined' && document.visibilityState === 'hidden';
  }

  function startPolling(): void {
    if (pollIntervalId) return;

    // Fetch immediately
    fetch();

    // Skip scheduled fetches while the tab is hidden — they just pile up CPU on the API
    // and get fired all at once on resume anyway.
    pollIntervalId = setInterval(() => {
      if (!isHidden()) fetch();
    }, pollInterval);

    // Catch-up fetch when the tab becomes visible again.
    if (typeof document !== 'undefined') {
      visibilityHandler = () => {
        if (!isHidden()) fetch();
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
  }

  function clear(): void {
    data = null;
    error = null;
    lastFetch = null;
  }

  return {
    get data() {
      return data;
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
    get count() {
      return count();
    },
    fetch,
    startPolling,
    stopPolling,
    clear
  };
}
