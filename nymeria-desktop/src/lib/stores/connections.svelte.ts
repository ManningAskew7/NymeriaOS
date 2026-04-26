import type { AccountIdentity, SavedConnection } from '$lib/types';
import { configStore } from '$lib/stores/config.svelte';
import { chatStore } from '$lib/stores/chat.svelte';
import { threadsStore } from '$lib/stores/threads.svelte';
import { autonomousStore } from '$lib/stores/autonomous.svelte';
import { notificationStore } from '$lib/stores/notifications.svelte';
import { stopSyncPoll } from '$lib/stores/syncPoll.svelte';

const CONNECTIONS_KEY = 'nymeria-saved-connections';
const ACTIVE_ID_KEY = 'nymeria-active-connection-id';

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
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(CONNECTIONS_KEY, JSON.stringify(connections));
  } catch (e) {
    console.error('Failed to save connections:', e);
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

function createConnectionsStore() {
  let connections = $state<SavedConnection[]>(loadConnections());
  let activeConnectionId = $state<string | null>(loadActiveId());
  let switching = $state(false);

  function persist() {
    saveConnections(connections);
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
        apiUrl,
        apiKey,
      };
      connections = [...connections, conn];
      persist();
      return conn;
    },

    update(id: string, updates: Partial<Pick<SavedConnection, 'name' | 'apiUrl' | 'apiKey'>>) {
      connections = connections.map((c) => (c.id === id ? { ...c, ...updates } : c));
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
      const conn = this.add(name, configStore.apiUrl, configStore.apiKey);
      activeConnectionId = conn.id;
      saveActiveId(conn.id);
      return conn;
    },

    async switchTo(id: string) {
      const conn = connections.find((c) => c.id === id);
      if (!conn) return;

      switching = true;
      try {
        // 1. Disconnect SSE and polling
        autonomousStore.disconnect();
        stopSyncPoll();
        notificationStore.stopPolling();

        // 2. Clear current state
        chatStore.clearMessages();

        // 3. Update config (triggers reactive updates in api service)
        configStore.apiUrl = conn.apiUrl;
        configStore.apiKey = conn.apiKey;

        // 4. Track active connection
        activeConnectionId = id;
        saveActiveId(id);

        // 5. Refresh identity FIRST so scoped-localStorage keys resolve
        // to the new user's namespace before threads I/O. Without this,
        // currentIdentityId stays stale and reset/sync read & write the
        // previous user's `nymeria-*-<old-id>` keys. .catch keeps the
        // switch going on a network/401 failure — better to land in the
        // new backend with a stale namespace than to abort mid-switch.
        await configStore.refreshIdentity().catch(() => {});

        // 6. Reset and reload threads from new backend
        threadsStore.reset();
        await threadsStore.syncFromBackend();

        // 6. Reconnect services
        autonomousStore.connect();
        notificationStore.startPolling();
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
