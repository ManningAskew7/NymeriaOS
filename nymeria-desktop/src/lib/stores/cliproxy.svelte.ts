/**
 * CLIProxy state management, backend-first.
 *
 * The backend's admin /cliproxy routes drive the proxy's management API
 * (OAuth logins, auth files, config knobs, apply-route), so everything here
 * works identically in thin-client mode against a remote backend. The Tauri
 * docker-compose controls remain only as the local-sidecar fallback for
 * desktop installs that manage their own proxy container.
 */

import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import type {
  CLIProxyAuthFile,
  CLIProxyProviderInfo,
  CLIProxyStatus
} from '$lib/types';

interface CLIProxySession {
  provider: string;
  email: string;
}

interface LocalProcessStatus {
  running: boolean;
  base_url?: string;
  detail?: string;
  sessions: CLIProxySession[];
}

export interface OAuthFlowState {
  provider: string;
  flow: 'browser' | 'device';
  url: string;
  state: string;
  status: 'wait' | 'ok' | 'error' | 'delivering';
  detail: string;
}

const POLL_INTERVAL_MS = 2000;
const LOGIN_TIMEOUT_MS = 600_000;

function createCLIProxyStore() {
  let status = $state<CLIProxyStatus | null>(null);
  let authFiles = $state<CLIProxyAuthFile[]>([]);
  let knobs = $state<Record<string, unknown>>({});
  let oauth = $state<OAuthFlowState | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let message = $state<string | null>(null);
  let oauthTimer: ReturnType<typeof setInterval> | null = null;

  // Local-sidecar fallback (Tauri only).
  let localRunning = $state(false);
  let localDetail = $state('');
  let localSessions = $state<CLIProxySession[]>([]);

  async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
    const { invoke: tauriInvoke } = await import('@tauri-apps/api/core');
    return tauriInvoke<T>(cmd, args);
  }

  function fail(e: unknown, action: 'load' | 'save' | 'connect' | 'update' | 'delete' | 'start', resource: string) {
    error = humanizeErrorText(e, { action, resource });
  }

  async function refresh(probe = false) {
    error = null;
    try {
      status = await api.getCLIProxyStatus(probe);
      if (status?.reachable) {
        authFiles = await api.listCLIProxyAuthFiles();
      } else {
        authFiles = [];
      }
    } catch (e) {
      fail(e, 'load', 'the CLIProxy status');
    }
    // The local Tauri process status is a best-effort extra; absence of the
    // Tauri runtime (web/thin-client builds) is normal.
    try {
      const local = await invoke<LocalProcessStatus>('get_cliproxy_status');
      localRunning = local.running;
      localDetail = local.detail || '';
      localSessions = local.sessions || [];
    } catch {
      localRunning = false;
      localSessions = [];
    }
  }

  async function loadKnobs() {
    try {
      knobs = await api.getCLIProxyConfig();
    } catch (e) {
      fail(e, 'load', 'the proxy settings');
    }
  }

  async function saveKnobs(update: Record<string, unknown>) {
    error = null;
    message = null;
    try {
      knobs = await api.patchCLIProxyConfig(update);
      message = 'Proxy settings saved.';
    } catch (e) {
      fail(e, 'save', 'the proxy settings');
    }
  }

  function stopOAuthPolling() {
    if (oauthTimer) {
      clearInterval(oauthTimer);
      oauthTimer = null;
    }
  }

  function startStatusPolling(providerId: string, providerLabel: string, oauthState: string, timeoutMs: number) {
    stopOAuthPolling();
    const deadline = Date.now() + timeoutMs;
    oauthTimer = setInterval(async () => {
      const current = oauth;
      if (!current || current.state !== oauthState) {
        stopOAuthPolling();
        return;
      }
      if (Date.now() > deadline) {
        stopOAuthPolling();
        oauth = { ...current, status: 'error', detail: 'The login session expired; start it again.' };
        return;
      }
      try {
        const result = await api.getCLIProxyOAuthStatus(oauthState, providerId);
        if (result === 'ok') {
          stopOAuthPolling();
          oauth = { ...current, status: 'ok', detail: 'Login complete.' };
          message = `${providerLabel} login complete.`;
          await refresh(true);
        } else if (result === 'error') {
          stopOAuthPolling();
          oauth = { ...current, status: 'error', detail: 'The provider reported a login error.' };
        }
      } catch (e) {
        stopOAuthPolling();
        oauth = { ...current, status: 'error', detail: humanizeErrorText(e, { action: 'connect', resource: 'the proxy' }) };
      }
    }, POLL_INTERVAL_MS);
  }

  async function startOAuth(provider: CLIProxyProviderInfo) {
    error = null;
    message = null;
    stopOAuthPolling();
    try {
      const started = await api.startCLIProxyOAuth(provider.id);
      oauth = {
        provider: provider.id,
        flow: started.flow,
        url: started.url,
        state: started.state,
        status: 'wait',
        detail:
          started.flow === 'device'
            ? 'Open the link and approve the login; this panel updates automatically.'
            : 'Approve the login in the browser. If it ends on a dead localhost page, paste that page\'s full URL below.'
      };
      window.open(started.url, '_blank', 'noopener');
      startStatusPolling(provider.id, provider.label, started.state, LOGIN_TIMEOUT_MS);
    } catch (e) {
      fail(e, 'start', 'the login');
    }
  }

  async function deliverCallback(redirectUrl: string) {
    const current = oauth;
    if (!current) return;
    error = null;
    oauth = { ...current, status: 'delivering', detail: 'Delivering the callback to the proxy...' };
    try {
      await api.deliverCLIProxyOAuthCallback(current.provider, redirectUrl);
      // The await may have raced the poll: never clobber a completed login,
      // and restart the poll if it already died (timeout/error before paste).
      const latest = oauth;
      if (!latest || latest.state !== current.state || latest.status === 'ok') return;
      oauth = { ...latest, status: 'wait', detail: 'Callback delivered; finishing the login...' };
      if (!oauthTimer) {
        const spec = status?.providers.find((entry) => entry.id === current.provider);
        startStatusPolling(current.provider, spec?.label ?? current.provider, current.state, 120_000);
      }
    } catch (e) {
      const latest = oauth;
      if (!latest || latest.state !== current.state || latest.status === 'ok') return;
      oauth = { ...latest, status: 'wait', detail: humanizeErrorText(e, { action: 'send', resource: 'the callback' }) };
    }
  }

  function dismissOAuth() {
    stopOAuthPolling();
    oauth = null;
  }

  async function setAuthFileDisabled(name: string, disabled: boolean) {
    error = null;
    try {
      await api.patchCLIProxyAuthFile(name, { disabled });
      await refresh();
    } catch (e) {
      fail(e, 'update', 'the login');
    }
  }

  async function deleteAuthFile(name: string) {
    error = null;
    try {
      await api.deleteCLIProxyAuthFile(name);
      message = 'Login removed from the proxy.';
      await refresh(true);
    } catch (e) {
      fail(e, 'delete', 'the login');
    }
  }

  async function applyRoute(
    provider: string,
    options: { model?: string; scope?: 'global' | 'thread'; threadId?: string; gatekeeperKey?: string } = {}
  ) {
    error = null;
    message = null;
    try {
      const applied = await api.applyCLIProxyRoute({
        provider,
        model: options.model,
        scope: options.scope ?? 'global',
        thread_id: options.threadId,
        gatekeeper_key: options.gatekeeperKey
      });
      message =
        applied.scope === 'thread'
          ? `Thread routed through CLIProxy (${applied.model}).`
          : `Backend route set to ${applied.provider} via CLIProxy (${applied.model}).`;
      return applied;
    } catch (e) {
      fail(e, 'save', 'the LLM route');
      return null;
    }
  }

  // --- local sidecar fallback (Tauri desktop only) -------------------------

  async function startLocal() {
    loading = true;
    error = null;
    message = null;
    try {
      await invoke('start_cliproxy');
      await new Promise((r) => setTimeout(r, 1000));
      await refresh(true);
      message = 'Local CLIProxy container started.';
    } catch (e) {
      fail(e, 'start', 'the local CLIProxy container');
    } finally {
      loading = false;
    }
  }

  async function stopLocal() {
    loading = true;
    error = null;
    message = null;
    try {
      await invoke('stop_cliproxy');
      localRunning = false;
      localSessions = [];
      message = 'Local CLIProxy container stopped.';
    } catch (e) {
      fail(e, 'update', 'the local CLIProxy container');
    } finally {
      loading = false;
    }
  }

  return {
    get status() {
      return status;
    },
    get providers(): CLIProxyProviderInfo[] {
      return status?.providers ?? [];
    },
    get configured() {
      return status?.configured ?? false;
    },
    get reachable() {
      return status?.reachable ?? false;
    },
    get managementHtmlUrl() {
      return status?.management_html_url ?? null;
    },
    get authFiles() {
      return authFiles;
    },
    get knobs() {
      return knobs;
    },
    get oauth() {
      return oauth;
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
    get localRunning() {
      return localRunning;
    },
    get localDetail() {
      return localDetail;
    },
    get localSessions() {
      return localSessions;
    },
    authFilesFor(providerId: string): CLIProxyAuthFile[] {
      const spec = status?.providers.find((entry) => entry.id === providerId);
      const fileProvider = spec?.auth_file_provider || providerId;
      return authFiles.filter(
        (file) => (file.provider || '').toLowerCase() === fileProvider
      );
    },
    refresh,
    loadKnobs,
    saveKnobs,
    startOAuth,
    deliverCallback,
    dismissOAuth,
    setAuthFileDisabled,
    deleteAuthFile,
    applyRoute,
    startLocal,
    stopLocal
  };
}

export const cliproxyStore = createCLIProxyStore();
