import { api } from '$lib/services/api.svelte';

const POLL_INTERVAL = 15000; // 15 seconds

function createHealthStore() {
  let connected = $state(false);
  let checking = $state(false);
  let lastCheck = $state<Date | null>(null);
  let latencyMs = $state<number | null>(null);
  let pollIntervalId: ReturnType<typeof setInterval> | null = null;

  async function check(): Promise<void> {
    checking = true;

    try {
      const start = performance.now();
      const ok = await api.healthCheck();
      const elapsed = Math.round(performance.now() - start);

      connected = ok;
      latencyMs = ok ? elapsed : null;
      lastCheck = new Date();
    } catch {
      connected = false;
      latencyMs = null;
    } finally {
      checking = false;
    }
  }

  function startPolling(): void {
    if (pollIntervalId) return;

    // Check immediately
    check();

    // Then poll every POLL_INTERVAL
    pollIntervalId = setInterval(() => check(), POLL_INTERVAL);
  }

  function stopPolling(): void {
    if (pollIntervalId) {
      clearInterval(pollIntervalId);
      pollIntervalId = null;
    }
  }

  return {
    get connected() {
      return connected;
    },
    get checking() {
      return checking;
    },
    get lastCheck() {
      return lastCheck;
    },
    get latencyMs() {
      return latencyMs;
    },
    startPolling,
    stopPolling,
    check
  };
}

export const healthStore = createHealthStore();
