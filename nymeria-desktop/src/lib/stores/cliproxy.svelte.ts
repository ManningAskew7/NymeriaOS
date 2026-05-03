/**
 * CLIProxy state management.
 * Wraps Tauri invoke() calls for the local sidecar process and uses the
 * frontend API client for backend settings changes.
 */

import { api } from '$lib/services/api.svelte';

interface CLIProxySession {
  provider: string;
  email: string;
}

interface CLIProxyStatusResponse {
  running: boolean;
  sessions: CLIProxySession[];
}

function createCLIProxyStore() {
  let running = $state(false);
  let sessions = $state<CLIProxySession[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let pollIntervalId: ReturnType<typeof setInterval> | null = null;

  async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
    const { invoke: tauriInvoke } = await import('@tauri-apps/api/core');
    return tauriInvoke<T>(cmd, args);
  }

  async function refreshStatus() {
    try {
      const status = await invoke<CLIProxyStatusResponse>('get_cliproxy_status');
      running = status.running;
      sessions = status.sessions;
      error = null;
    } catch (e) {
      error = String(e);
    }
  }

  async function start() {
    loading = true;
    error = null;
    try {
      await invoke('start_cliproxy');
      // Wait a moment for the process to start, then refresh
      await new Promise((r) => setTimeout(r, 1000));
      await refreshStatus();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function stop() {
    loading = true;
    error = null;
    try {
      await invoke('stop_cliproxy');
      running = false;
      sessions = [];
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function login(provider: 'claude' | 'openai') {
    error = null;
    try {
      await invoke('cliproxy_login', { provider });
    } catch (e) {
      error = String(e);
    }
  }

  async function applyBaseUrl() {
    error = null;
    try {
      await api.updateServerSettings({
        llm_base_url: 'http://127.0.0.1:8317/v1'
      });
    } catch (e) {
      error = String(e);
    }
  }

  async function removeBaseUrl() {
    error = null;
    try {
      await api.updateServerSettings({
        llm_base_url: null
      });
    } catch (e) {
      error = String(e);
    }
  }

  function startPolling() {
    if (pollIntervalId) return;
    refreshStatus();
    pollIntervalId = setInterval(refreshStatus, 5000);
  }

  function stopPolling() {
    if (pollIntervalId) {
      clearInterval(pollIntervalId);
      pollIntervalId = null;
    }
  }

  return {
    get running() {
      return running;
    },
    get sessions() {
      return sessions;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    get needsAuth() {
      return running && sessions.length === 0;
    },
    refreshStatus,
    start,
    stop,
    login,
    applyBaseUrl,
    removeBaseUrl,
    startPolling,
    stopPolling,
  };
}

export const cliproxyStore = createCLIProxyStore();
