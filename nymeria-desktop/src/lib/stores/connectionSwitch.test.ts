import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

// #242 integration: a real switch through connectionsStore.applyConnection,
// with the REAL config, serverSettings, threads and threadConfig stores wired
// by their real reload hooks. Only the boundaries are stubbed: localStorage,
// fetch (GET /me), the typed api client, and the SSE/polling services the
// orchestrator stops and restarts. Both backends name their owner `default`,
// which is exactly the case the old id-only trip-wire never noticed.

const env = vi.hoisted(() => {
  class MemoryStorage {
    data = new Map<string, string>();
    get length() {
      return this.data.size;
    }
    key(i: number) {
      return [...this.data.keys()][i] ?? null;
    }
    getItem(key: string) {
      return this.data.get(key) ?? null;
    }
    setItem(key: string, value: string) {
      this.data.set(key, String(value));
    }
    removeItem(key: string) {
      this.data.delete(key);
    }
  }
  const A = 'http://localhost:8097';
  const storage = new MemoryStorage();
  // The config store reads its blob at import.
  storage.setItem(
    'nymeria-config',
    JSON.stringify({
      apiUrl: A,
      apiKey: 'nym_a',
      setupCompleted: true,
      theme: 'light',
      identity: { id: 'default', email: 'default@localhost', display_name: 'Owner', role: 'admin' },
    }),
  );
  (globalThis as { localStorage?: unknown }).localStorage = storage;
  // GET /me per backend URL: an account id, or 'down' for a network error.
  const me: Record<string, string> = {};
  (globalThis as { fetch?: unknown }).fetch = async (url: string) => {
    const base = Object.keys(me).find((b) => url.startsWith(`${b}/`));
    const answer = base ? me[base] : 'down';
    if (answer === 'down') throw new TypeError('Failed to fetch');
    return new Response(
      JSON.stringify({ id: answer, email: `${answer}@localhost`, display_name: answer, role: 'admin' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  };
  return { storage, me, A };
});

vi.mock('$lib/services/secureStorage', () => ({
  secureGet: vi.fn().mockResolvedValue(null),
  secureSet: vi.fn().mockResolvedValue(undefined),
  secureDelete: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getServerSettings: vi.fn(),
    listThreadsWithMetadata: vi.fn(),
    listThreadTeams: vi.fn(),
    getThreadConfig: vi.fn(),
  },
}));
vi.mock('$lib/stores/chat.svelte', () => ({ chatStore: { clearMessages: vi.fn() } }));
vi.mock('$lib/stores/autonomous.svelte', () => ({
  autonomousStore: { disconnect: vi.fn(), connect: vi.fn() },
}));
vi.mock('$lib/stores/notifications.svelte', () => ({
  notificationStore: { stopPolling: vi.fn(), startPolling: vi.fn() },
}));
vi.mock('$lib/stores/syncPoll.svelte', () => ({ stopSyncPoll: vi.fn() }));

import { createConnectionsStore } from './connections.svelte';
import { configStore, currentIdentityScope, scopedKey } from './config.svelte';
import { serverSettingsStore } from './serverSettings.svelte';
import { threadsStore } from './threads.svelte';
import { threadConfigStore } from './threadConfig.svelte';
import { api } from '$lib/services/api.svelte';

const A = env.A;
const B = 'http://localhost:8098';
const THREAD = 'telegram_5559876543';

function settings(model: string) {
  return {
    llm_provider: 'anthropic',
    llm_provider_route: 'default',
    llm_model: model,
    llm_fast_model_resolved: `${model}-fast`,
    llm_smart_model_resolved: `${model}-smart`,
    memory_char_limit: 4000,
  };
}

function threadRow(id: string, title: string) {
  return {
    thread_id: id,
    title,
    pinned: false,
    platform: 'desktop',
    platform_meta: null,
    created_at: null,
    updated_at: null,
    title_source: 'user',
  };
}

const connections = createConnectionsStore();

/** Settle the app on backend A with A's settings, threads and a cached thread config. */
async function settleOnA() {
  env.me[A] = 'default';
  env.me[B] = 'default';
  await connections.applyConnection(A, 'nym_a');
  (api.getServerSettings as Mock).mockResolvedValueOnce(settings('model-a'));
  await serverSettingsStore.load();
  (api.getThreadConfig as Mock).mockResolvedValueOnce({ threadId: THREAD, instructions: 'A instructions' });
  await threadConfigStore.loadConfig(THREAD);
  expect(serverSettingsStore.model).toBe('model-a');
  expect(threadConfigStore.getConfig(THREAD)?.instructions).toBe('A instructions');
  expect(currentIdentityScope()?.backend).toBe(A);
}

beforeEach(async () => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow(THREAD, 'Shared id')] });
  (api.listThreadTeams as Mock).mockResolvedValue([]);
  await settleOnA();
});

describe('applyConnection A to B, both owners `default`', () => {
  it('drops A`s server settings and thread config before B`s load, then shows B`s', async () => {
    await connections.applyConnection(B, 'nym_b');

    expect(configStore.identity?.id).toBe('default');
    expect(currentIdentityScope()?.backend).toBe(B);
    expect(serverSettingsStore.model).toBeNull();
    expect(serverSettingsStore.fastModelResolved).toBeNull();
    expect(serverSettingsStore.loaded).toBe(false);
    expect(threadConfigStore.getConfig(THREAD)).toBeUndefined();
    expect(threadsStore.currentThreadId).toBeNull();

    (api.getServerSettings as Mock).mockResolvedValueOnce(settings('model-b'));
    await serverSettingsStore.load();
    expect(serverSettingsStore.model).toBe('model-b');
    expect(serverSettingsStore.smartModelResolved).toBe('model-b-smart');
  });

  it('a re-sign-in on the SAME backend (new token, same account) still resets: every switch is a switch', async () => {
    await connections.applyConnection(A, 'nym_a_rotated');

    expect(currentIdentityScope()?.backend).toBe(A);
    expect(serverSettingsStore.model).toBeNull();
    expect(serverSettingsStore.loaded).toBe(false);
    expect(threadConfigStore.getConfig(THREAD)).toBeUndefined();
  });

  it('when B`s /me fails with a network error the switch lands empty and unscoped, never on A`s values', async () => {
    env.me[B] = 'down';
    await connections.applyConnection(B, 'nym_b');

    expect(configStore.apiUrl).toBe(B);
    expect(configStore.identity).toBeNull();
    expect(currentIdentityScope()).toBeNull();
    expect(scopedKey('nymeria-thread-folders')).toBe('nymeria-thread-folders');
    expect(serverSettingsStore.model).toBeNull();
    expect(serverSettingsStore.loaded).toBe(false);
    expect(threadConfigStore.getConfig(THREAD)).toBeUndefined();
  });
});

describe('local-only thread folders are per backend', () => {
  it('a folder made on A is absent on B and back again on A', async () => {
    threadsStore.createFolder('Work on A');
    expect(threadsStore.folders.map((f) => f.name)).toEqual(['Work on A']);

    await connections.applyConnection(B, 'nym_b');
    expect(threadsStore.folders).toEqual([]);
    threadsStore.createFolder('Home on B');

    await connections.applyConnection(A, 'nym_a');
    expect(threadsStore.folders.map((f) => f.name)).toEqual(['Work on A']);

    await connections.applyConnection(B, 'nym_b');
    expect(threadsStore.folders.map((f) => f.name)).toEqual(['Home on B']);

    // Cleanup for the next test: A's folders do not leak into its baseline.
    for (const f of threadsStore.folders) threadsStore.deleteFolder(f.id);
    await connections.applyConnection(A, 'nym_a');
    for (const f of threadsStore.folders) threadsStore.deleteFolder(f.id);
  });
});
