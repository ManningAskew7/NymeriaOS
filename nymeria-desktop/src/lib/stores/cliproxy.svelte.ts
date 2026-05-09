/**
 * CLIProxy state management.
 * Wraps Tauri invoke() calls for the local sidecar process and uses the
 * frontend API client for backend settings changes.
 */

import { api } from '$lib/services/api.svelte';
import type { ServerSettingsUpdate } from '$lib/types';

export const LOCAL_CLIPROXY_ROOT_URL = 'http://127.0.0.1:8318';
export const LOCAL_OPENAI_CLIPROXY_BASE_URL = `${LOCAL_CLIPROXY_ROOT_URL}/v1`;
export const CLIPROXY_CLAUDE_MODEL = 'claude-opus-4-7';
export const CLIPROXY_CODEX_MODEL = 'gpt-5.5';

interface CLIProxySession {
  provider: string;
  email: string;
}

interface CLIProxyStatusResponse {
  running: boolean;
  base_url?: string;
  detail?: string;
  sessions: CLIProxySession[];
}

function createCLIProxyStore() {
  let running = $state(false);
  let sessions = $state<CLIProxySession[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let message = $state<string | null>(null);
  let baseUrl = $state(LOCAL_CLIPROXY_ROOT_URL);
  let detail = $state('');
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
      baseUrl = status.base_url || LOCAL_CLIPROXY_ROOT_URL;
      detail = status.detail || '';
      error = null;
    } catch (e) {
      error = String(e);
    }
  }

  async function start() {
    loading = true;
    error = null;
    message = null;
    try {
      await invoke('start_cliproxy');
      // Wait a moment for the process to start, then refresh
      await new Promise((r) => setTimeout(r, 1000));
      await refreshStatus();
      message = 'CLIProxy started from the pinned Docker compose deployment.';
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function stop() {
    loading = true;
    error = null;
    message = null;
    try {
      await invoke('stop_cliproxy');
      running = false;
      sessions = [];
      message = 'CLIProxy stopped.';
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function login(provider: 'claude' | 'openai') {
    error = null;
    message = null;
    try {
      await invoke('cliproxy_login', { provider });
      message = provider === 'claude'
        ? 'Claude OAuth login command started.'
        : 'Codex/OpenAI OAuth login command started.';
    } catch (e) {
      error = String(e);
    }
  }

  async function updateRoute(updates: ServerSettingsUpdate, successMessage: string) {
    error = null;
    message = null;
    try {
      await api.updateServerSettings(updates);
      message = successMessage;
    } catch (e) {
      error = String(e);
    }
  }

  async function applyClaudeRoute() {
    await updateRoute(
      {
        llm_provider: 'anthropic',
        llm_model: CLIPROXY_CLAUDE_MODEL,
        llm_base_url: LOCAL_CLIPROXY_ROOT_URL,
      },
      'Backend route set to CLIProxy Claude OAuth. Confirm the Anthropic gatekeeper key is configured before chatting.'
    );
  }

  async function applyCodexRoute() {
    await updateRoute(
      {
        llm_provider: 'openai',
        llm_model: CLIPROXY_CODEX_MODEL,
        llm_base_url: LOCAL_OPENAI_CLIPROXY_BASE_URL,
        openai_api_mode: 'responses',
      },
      'Backend route set to CLIProxy Codex/OpenAI OAuth with Responses API mode. Confirm the OpenAI gatekeeper key is configured before chatting.'
    );
  }

  async function useDirectApi() {
    await updateRoute(
      { llm_base_url: '' },
      'Backend base URL override cleared.'
    );
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
    get message() {
      return message;
    },
    get baseUrl() {
      return baseUrl;
    },
    get detail() {
      return detail;
    },
    get needsAuth() {
      return running && sessions.length === 0;
    },
    get claudeSessions() {
      return sessions.filter((session) => session.provider.toLowerCase() === 'claude');
    },
    get openAISessions() {
      return sessions.filter((session) => {
        const provider = session.provider.toLowerCase();
        return provider === 'openai' || provider === 'codex';
      });
    },
    refreshStatus,
    start,
    stop,
    login,
    applyClaudeRoute,
    applyCodexRoute,
    useDirectApi,
    startPolling,
    stopPolling,
  };
}

export const cliproxyStore = createCLIProxyStore();
