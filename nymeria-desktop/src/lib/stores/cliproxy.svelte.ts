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
// Matches the backend's SESSION_OK_GUARD_SECONDS (management_client.py):
// polling past the window where a paste-less "ok" is still trusted could
// only surface the stale-session refusal, so the poller stops at the
// neutral expiry message instead.
const LOGIN_TIMEOUT_MS = 540_000;

function createCLIProxyStore() {
  let status = $state<CLIProxyStatus | null>(null);
  let authFiles = $state<CLIProxyAuthFile[]>([]);
  let knobs = $state<Record<string, unknown>>({});
  let models = $state<{ id: string; owned_by: string }[]>([]);
  let oauth = $state<OAuthFlowState | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let message = $state<string | null>(null);
  let oauthTimer: ReturnType<typeof setInterval> | null = null;
  // Absolute polling deadline anchored at startOAuth: re-arming the poller
  // (after a paste) must never quietly extend a login past its window with
  // no new evidence. A SUCCESSFUL delivery is evidence (the proxy accepted
  // the callback, so the session was alive) and buys a short finishing
  // window; a FAILED delivery proves nothing and keeps the original clock.
  let oauthDeadline = 0;

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

  async function loadModels() {
    // Live ids through the proxy (all logged-in subscriptions, no
    // per-provider attribution); [] on failure keeps the model inputs
    // free-text only, so this never blocks the panel.
    models = await api.listCLIProxyModels();
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

  function startStatusPolling(providerId: string, providerLabel: string, oauthState: string) {
    stopOAuthPolling();
    oauthTimer = setInterval(async () => {
      const current = oauth;
      if (!current || current.state !== oauthState) {
        stopOAuthPolling();
        return;
      }
      if (Date.now() > oauthDeadline) {
        stopOAuthPolling();
        oauth = { ...current, status: 'error', detail: 'The login session expired; start it again.' };
        return;
      }
      try {
        const result = await api.getCLIProxyOAuthStatus(oauthState, providerId);
        if (result.status === 'ok') {
          stopOAuthPolling();
          oauth = {
            ...current,
            status: 'ok',
            detail: result.detail ? `Logged in as ${result.detail}.` : 'Login complete.'
          };
          message = `${providerLabel} login complete.`;
          // Fresh auth files only (see deleteAuthFile on why not a probe).
          await refresh();
        } else if (result.status === 'error') {
          stopOAuthPolling();
          // Prefer the backend's explanation (notably the confirmed-ok trap:
          // the proxy answers ok for expired sessions and the backend refuses
          // it with a message far more useful than a generic error line).
          oauth = {
            ...current,
            status: 'error',
            detail: result.detail || 'The provider reported a login error.'
          };
        }
      } catch (e) {
        stopOAuthPolling();
        oauth = { ...current, status: 'error', detail: humanizeErrorText(e, { action: 'connect', resource: 'the proxy' }) };
      }
    }, POLL_INTERVAL_MS);
  }

  async function openExternal(href: string) {
    // Packaged (Tauri) builds cannot window.open; use the opener plugin
    // with a plain-browser fallback (AuthPromptModal precedent).
    try {
      const { openUrl } = await import('@tauri-apps/plugin-opener');
      await openUrl(href);
    } catch {
      window.open(href, '_blank', 'noopener,noreferrer');
    }
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
      void openExternal(started.url);
      oauthDeadline = Date.now() + LOGIN_TIMEOUT_MS;
      startStatusPolling(provider.id, provider.label, started.state);
    } catch (e) {
      fail(e, 'start', 'the login');
    }
  }

  async function deliverCallback(redirectUrl: string) {
    const current = oauth;
    if (!current) return;
    error = null;
    oauth = { ...current, status: 'delivering', detail: 'Delivering the callback to the proxy…' };
    try {
      await api.deliverCLIProxyOAuthCallback(current.provider, redirectUrl);
      // The await may have raced the poll: never clobber a completed login,
      // and restart the poll if it already died (timeout/error before paste).
      const latest = oauth;
      if (!latest || latest.state !== current.state || latest.status === 'ok') return;
      oauth = { ...latest, status: 'wait', detail: 'Callback delivered; finishing the login…' };
      // An accepted callback proves the session was alive: extend the
      // window enough to finish confirming, never shrinking it.
      oauthDeadline = Math.max(oauthDeadline, Date.now() + 120_000);
      if (!oauthTimer) {
        const spec = status?.providers.find((entry) => entry.id === current.provider);
        startStatusPolling(current.provider, spec?.label ?? current.provider, current.state);
      }
    } catch (e) {
      const latest = oauth;
      if (!latest || latest.state !== current.state || latest.status === 'ok') return;
      // A failed delivery proves nothing about session liveness, so it may
      // revive a dead poller (or the panel could never confirm a retry)
      // only WITHIN the original window, never extending it.
      if (Date.now() > oauthDeadline) {
        stopOAuthPolling();
        oauth = { ...latest, status: 'error', detail: 'The login session expired; start it again.' };
        return;
      }
      oauth = { ...latest, status: 'wait', detail: humanizeErrorText(e, { action: 'send', resource: 'the callback' }) };
      if (!oauthTimer) {
        const spec = status?.providers.find((entry) => entry.id === current.provider);
        startStatusPolling(current.provider, spec?.label ?? current.provider, current.state);
      }
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

  async function importAuthFile(provider: string, name: string, content: string) {
    error = null;
    message = null;
    try {
      const result = await api.importCLIProxyAuthFile(provider, name, content);
      // Refresh FIRST: it clears `error` as its opening move, so the
      // outcome message must be assigned after it or the inactive detail
      // would be silently wiped (silence after a click reads as success).
      await refresh();
      if (result.status === 'ok') {
        message = result.account
          ? `Imported ${name}: active login as ${result.account}.`
          : `Imported ${name}: login active.`;
      } else {
        // Accepted-but-inactive is not a success; render the backend's
        // honest explanation inline.
        error = result.detail || `The proxy accepted ${name} but lists no active login.`;
      }
      return result.status === 'ok';
    } catch (e) {
      fail(e, 'save', 'the auth file');
      return false;
    }
  }

  async function deleteAuthFile(name: string) {
    error = null;
    try {
      await api.deleteCLIProxyAuthFile(name);
      message = 'Login removed from the proxy.';
      // Fresh auth files, not a fresh capability probe: a forced probe
      // registers one dead pending OAuth session per catalog provider on
      // the proxy, and the binary's capabilities did not change here.
      await refresh();
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
      if (applied.restart_required) {
        // The five-surface restart idiom (ProviderSection etc.): today the
        // apply-route fields all hot-reload, so this is future-proofing.
        message += ' Restart the backend for every change to take effect.';
      }
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
    get models() {
      return models;
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
    loadModels,
    saveKnobs,
    startOAuth,
    deliverCallback,
    dismissOAuth,
    setAuthFileDisabled,
    importAuthFile,
    deleteAuthFile,
    applyRoute,
    startLocal,
    stopLocal
  };
}

export const cliproxyStore = createCLIProxyStore();
