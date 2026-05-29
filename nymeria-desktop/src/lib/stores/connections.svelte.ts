import type { AccountIdentity, SavedConnection } from '$lib/types';
import { configStore } from '$lib/stores/config.svelte';
import { chatStore } from '$lib/stores/chat.svelte';
import { threadsStore } from '$lib/stores/threads.svelte';
import { autonomousStore } from '$lib/stores/autonomous.svelte';
import { notificationStore } from '$lib/stores/notifications.svelte';
import { stopSyncPoll } from '$lib/stores/syncPoll.svelte';
import { secureGet, secureSet } from '$lib/services/secureStorage';

const CONNECTIONS_KEY = 'nymeria-saved-connections';
// H-7: the full saved-connection list (including per-account apiKeys) lives in
// the OS keychain under this key. CONNECTIONS_KEY keeps only a redacted copy
// (apiKey blanked) for a fast first paint before async hydration completes.
const CONNECTIONS_KEYCHAIN_KEY = 'saved-connections';
const ACTIVE_ID_KEY = 'nymeria-active-connection-id';

function normalizeApiUrl(url: string): string {
  return url.trim().replace(/\/+$/, '');
}

function safeHostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

function identityLabel(identity: AccountIdentity | null | undefined): string | null {
  if (!identity) return null;
  const displayName = identity.display_name?.trim();
  if (displayName) return displayName;
  const email = identity.email?.trim();
  return email || null;
}

function loadConnections(): SavedConnection[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const stored = localStorage.getItem(CONNECTIONS_KEY);
    if (stored) return JSON.parse(stored);
  } catch (e) {
    console.error('Failed to load saved connections:', e);
  }
  return [];
}

function saveConnections(connections: SavedConnection[]): void {
  // Full list (with apiKeys) -> OS keychain (or secureStorage's localStorage
  // fallback when the keychain is unavailable, e.g. a browser dev preview).
  void secureSet(CONNECTIONS_KEYCHAIN_KEY, JSON.stringify(connections));
  if (typeof localStorage === 'undefined') return;
  try {
    // Redacted breadcrumb for first paint: names/urls only, never the token.
    const redacted = connections.map((c) => ({ ...c, apiKey: '' }));
    localStorage.setItem(CONNECTIONS_KEY, JSON.stringify(redacted));
  } catch (e) {
    console.error('Failed to save connections metadata:', e);
  }
}

function loadActiveId(): string | null {
  if (typeof localStorage === 'undefined') return null;
  try {
    return localStorage.getItem(ACTIVE_ID_KEY);
  } catch {
    return null;
  }
}

function saveActiveId(id: string | null): void {
  if (typeof localStorage === 'undefined') return;
  try {
    if (id) {
      localStorage.setItem(ACTIVE_ID_KEY, id);
    } else {
      localStorage.removeItem(ACTIVE_ID_KEY);
    }
  } catch (e) {
    console.error('Failed to save active connection ID:', e);
  }
}

export function createConnectionsStore() {
  let connections = $state<SavedConnection[]>(loadConnections());
  let activeConnectionId = $state<string | null>(loadActiveId());
  let switching = $state(false);

  function persist() {
    saveConnections(connections);
  }

  // Load full connections (with apiKeys) from the OS keychain after the
  // synchronous redacted-metadata init. Migrates a legacy localStorage list
  // that still held inline apiKeys, then redacts the plaintext copy. The live
  // token comes from configStore (also keychain-backed), so a brief empty-token
  // window here only delays the saved-account list, not authentication.
  async function hydrateConnections(): Promise<void> {
    let stored: string | null = null;
    try {
      stored = await secureGet(CONNECTIONS_KEYCHAIN_KEY);
    } catch (e) {
      console.error('Failed to hydrate connections from secure storage:', e);
    }
    if (stored) {
      try {
        connections = JSON.parse(stored) as SavedConnection[];
        return;
      } catch (e) {
        console.error('Failed to parse stored connections:', e);
      }
    }
    const legacy = loadConnections();
    if (legacy.some((c) => c.apiKey)) {
      connections = legacy;
      saveConnections(legacy);
    }
  }

  void hydrateConnections();

  function findCredentialIndex(
    apiUrl: string,
    apiKey: string,
    identity?: AccountIdentity | null
  ): number {
    const normalizedUrl = normalizeApiUrl(apiUrl);
    const trimmedKey = apiKey.trim();

    if (identity) {
      const byIdentity = connections.findIndex(
        (c) => normalizeApiUrl(c.apiUrl) === normalizedUrl && c.identity?.id === identity.id
      );
      if (byIdentity >= 0) return byIdentity;
    }

    return connections.findIndex(
      (c) => normalizeApiUrl(c.apiUrl) === normalizedUrl && c.apiKey === trimmedKey
    );
  }

  function upsertAccountCredential(args: {
    apiUrl: string;
    apiKey: string;
    name?: string;
    identity?: AccountIdentity | null;
    makeActive?: boolean;
  }): SavedConnection {
    const apiUrl = normalizeApiUrl(args.apiUrl);
    const apiKey = args.apiKey.trim();
    const explicitName = args.name?.trim();
    const fallbackName = identityLabel(args.identity) ?? safeHostname(apiUrl) ?? 'Nymeria account';
    const name = explicitName || fallbackName;
    const checkedAt = args.identity !== undefined ? new Date().toISOString() : undefined;
    const existingIndex = findCredentialIndex(apiUrl, apiKey, args.identity);

    if (existingIndex >= 0) {
      const existing = connections[existingIndex];
      const updated: SavedConnection = {
        ...existing,
        name: explicitName || existing.name || name,
        apiUrl,
        apiKey,
        ...(args.identity !== undefined
          ? {
              identity: args.identity,
              identityCheckedAt: checkedAt,
              identityError: args.identity ? null : existing.identityError ?? null,
            }
          : {}),
      };
      connections = connections.map((c, i) => (i === existingIndex ? updated : c));
      if (args.makeActive) {
        activeConnectionId = updated.id;
        saveActiveId(updated.id);
      }
      persist();
      return updated;
    }

    const conn: SavedConnection = {
      id: crypto.randomUUID(),
      name,
      apiUrl,
      apiKey,
      ...(args.identity !== undefined
        ? {
            identity: args.identity,
            identityCheckedAt: checkedAt,
            identityError: args.identity ? null : null,
          }
        : {}),
    };
    connections = [...connections, conn];
    if (args.makeActive) {
      activeConnectionId = conn.id;
      saveActiveId(conn.id);
    }
    persist();
    return conn;
  }

  /**
   * Apply an arbitrary backend (url + token) as the live connection: tear down
   * the current session, repoint configStore, re-resolve identity, and clear +
   * re-sync threads against the new backend. This is the single source of truth
   * for switching backends — every UI path that commits a backend change (the
   * "Connect" button via switchTo, plus the Backend settings form's Save/edit
   * handlers) must route through here. The thread cache is scoped only by
   * user_id, so when two backends share an identity (e.g. both `default`) the
   * stale cache is overwritten only by this explicit reset + resync.
   */
  async function applyConnection(apiUrl: string, apiKey: string): Promise<void> {
    // 1. Disconnect SSE and polling
    autonomousStore.disconnect();
    stopSyncPoll();
    notificationStore.stopPolling();

    // 2. Clear current chat state
    chatStore.clearMessages();

    // 3. Update config (triggers reactive updates in the api service)
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;

    // 4. Reconcile the active saved connection: pin a matching saved entry if
    //    one exists, otherwise no saved entry owns this backend.
    const matchIndex = findCredentialIndex(apiUrl, apiKey);
    if (matchIndex >= 0) {
      activeConnectionId = connections[matchIndex].id;
      saveActiveId(activeConnectionId);
    } else {
      activeConnectionId = null;
      saveActiveId(null);
    }

    // 5. Refresh identity FIRST so scoped-localStorage keys resolve to the new
    //    user's namespace before threads I/O. .catch keeps the switch going on a
    //    network/401 failure — better to land in the new backend with a stale
    //    namespace than to abort mid-switch.
    await configStore.refreshIdentity().catch(() => {});

    // 6. Reset and reload threads from the new backend
    threadsStore.reset();
    await threadsStore.syncFromBackend();

    // 7. Reconnect services
    autonomousStore.connect();
    notificationStore.startPolling();
  }

  return {
    get connections() {
      return connections;
    },
    get activeConnectionId() {
      return activeConnectionId;
    },
    get activeConnection(): SavedConnection | null {
      if (!activeConnectionId) return null;
      return connections.find((c) => c.id === activeConnectionId) ?? null;
    },
    get switching() {
      return switching;
    },

    add(name: string, apiUrl: string, apiKey: string): SavedConnection {
      const conn: SavedConnection = {
        id: crypto.randomUUID(),
        name,
        apiUrl: normalizeApiUrl(apiUrl),
        apiKey: apiKey.trim(),
      };
      connections = [...connections, conn];
      persist();
      return conn;
    },

    upsertAccountCredential,

    update(id: string, updates: Partial<Pick<SavedConnection, 'name' | 'apiUrl' | 'apiKey'>>) {
      connections = connections.map((c) =>
        c.id === id
          ? {
              ...c,
              ...updates,
              apiUrl: updates.apiUrl !== undefined ? normalizeApiUrl(updates.apiUrl) : c.apiUrl,
              apiKey: updates.apiKey !== undefined ? updates.apiKey.trim() : c.apiKey,
            }
          : c
      );
      persist();
    },

    delete(id: string) {
      connections = connections.filter((c) => c.id !== id);
      persist();
      if (activeConnectionId === id) {
        activeConnectionId = null;
        saveActiveId(null);
      }
    },

    saveCurrentAs(name: string): SavedConnection {
      return upsertAccountCredential({
        name,
        apiUrl: configStore.apiUrl,
        apiKey: configStore.apiKey,
        identity: configStore.identity,
        makeActive: true,
      });
    },

    async ensureCurrentSaved(name?: string): Promise<SavedConnection | null> {
      if (!configStore.apiUrl || !configStore.apiKey) return null;
      let identity = configStore.identity;
      if (!identity) {
        identity = await configStore.refreshIdentity().catch(() => null);
      }
      return upsertAccountCredential({
        name,
        apiUrl: configStore.apiUrl,
        apiKey: configStore.apiKey,
        identity,
        makeActive: true,
      });
    },

    applyConnection,

    async switchTo(id: string) {
      const conn = connections.find((c) => c.id === id);
      if (!conn) return;

      switching = true;
      try {
        const sameBackend = normalizeApiUrl(configStore.apiUrl) === normalizeApiUrl(conn.apiUrl);
        const currentIsTarget =
          sameBackend &&
          (configStore.apiKey === conn.apiKey ||
            (!!configStore.identity && configStore.identity.id === conn.identity?.id));
        if (configStore.apiUrl && configStore.apiKey && !currentIsTarget) {
          upsertAccountCredential({
            apiUrl: configStore.apiUrl,
            apiKey: configStore.apiKey,
            identity: configStore.identity,
            makeActive: false,
          });
        }

        await applyConnection(conn.apiUrl, conn.apiKey);

        // An id-based switch always pins that entry, even if findCredentialIndex
        // matched a different saved entry sharing the same url + token.
        activeConnectionId = id;
        saveActiveId(id);
      } catch (e) {
        console.error('[Connections] Switch failed:', e);
      } finally {
        switching = false;
      }
    },

    clearActive() {
      activeConnectionId = null;
      saveActiveId(null);
    },

    /**
     * Hit GET /me against an entry's URL+token to resolve the real account
     * identity, then cache the result on the entry. Called on app boot for
     * the active entry (so the badge shows immediately), and on demand from
     * the AccountSwitcher when the user wants to refresh stale info.
     */
    async verifyEntry(id: string): Promise<AccountIdentity | null> {
      const entry = connections.find((c) => c.id === id);
      if (!entry) return null;
      // Token may not be hydrated from the keychain yet at boot; skip rather
      // than probe /me with an empty bearer and spuriously mark it revoked.
      if (!entry.apiKey) return null;
      const base = entry.apiUrl.replace(/\/$/, '');
      try {
        const response = await fetch(`${base}/me`, {
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${entry.apiKey}`,
          },
        });
        const checkedAt = new Date().toISOString();
        if (!response.ok) {
          const errMessage =
            response.status === 401
              ? 'Token revoked or expired'
              : response.status === 403
              ? 'Account disabled'
              : `Auth check failed (${response.status})`;
          connections = connections.map((c) =>
            c.id === id
              ? { ...c, identity: null, identityCheckedAt: checkedAt, identityError: errMessage }
              : c
          );
          persist();
          return null;
        }
        const data = (await response.json()) as AccountIdentity;
        connections = connections.map((c) =>
          c.id === id
            ? { ...c, identity: data, identityCheckedAt: checkedAt, identityError: null }
            : c
        );
        persist();
        return data;
      } catch (e) {
        connections = connections.map((c) =>
          c.id === id
            ? {
                ...c,
                identity: null,
                identityCheckedAt: new Date().toISOString(),
                identityError: e instanceof Error ? e.message : 'Network error',
              }
            : c
        );
        persist();
        return null;
      }
    },
  };
}

export const connectionsStore = createConnectionsStore();

// On boot, refresh the active entry's identity in the background so the badge
// reflects the current account state (in case the token was revoked, role
// changed, or account was disabled while we were away).
if (typeof window !== 'undefined') {
  setTimeout(() => {
    const activeId = connectionsStore.activeConnectionId;
    if (activeId) {
      void connectionsStore.verifyEntry(activeId);
    }
  }, 500);
}
