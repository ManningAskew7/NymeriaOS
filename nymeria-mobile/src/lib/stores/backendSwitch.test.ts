import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

// #242, mobile: Settings > Connection is the app's one switch surface. Save
// with a changed connection must run the full switch (the same sequence as
// desktop's applyConnection); Save with the live values must touch nothing;
// Test must validate the FORM without repointing or persisting the live
// connection. Real config, threads and serverSettings stores wired by their
// real reload hooks; stubbed boundaries: localStorage, fetch (/health, /me),
// the typed api client, and the SSE/polling/backup services.

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
  // Per backend URL: the /me account id, '401', or 'down' (network error).
  const me: Record<string, string> = {};
  const fetchCalls: string[] = [];
  (globalThis as { fetch?: unknown }).fetch = async (url: string) => {
    fetchCalls.push(url);
    const base = Object.keys(me).find((b) => url.startsWith(`${b}/`));
    const answer = base ? me[base] : 'down';
    if (answer === 'down') throw new TypeError('Failed to fetch');
    if (url.endsWith('/health')) {
      return new Response(JSON.stringify({ status: 'ok' }), { status: 200 });
    }
    if (answer === '401') return new Response('{}', { status: 401 });
    return new Response(
      JSON.stringify({ id: answer, email: `${answer}@localhost`, display_name: answer, role: 'admin' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  };
  return { storage, me, fetchCalls, A };
});

vi.mock('$lib/themes', () => ({ applyTheme: vi.fn() }));
vi.mock('$lib/services/api.svelte', async () => {
  const base = await vi.importActual<typeof import('$lib/services/api/base')>('$lib/services/api/base');
  return {
    probeConnection: base.probeConnection,
    api: {
      getServerSettings: vi.fn(),
      listThreadsWithMetadata: vi.fn(),
    },
  };
});
vi.mock('./chat.svelte', () => ({ chatStore: { clearMessages: vi.fn() } }));
vi.mock('./autonomous.svelte', () => ({ autonomousStore: { disconnect: vi.fn(), connect: vi.fn() } }));
vi.mock('./health.svelte', () => ({ healthStore: { stopPolling: vi.fn(), startPolling: vi.fn() } }));
vi.mock('./notifications.svelte', () => ({
  notificationStore: { stopPolling: vi.fn(), startPolling: vi.fn() },
}));
vi.mock('$lib/utils/lifecycle', () => ({ backupToPreferences: vi.fn().mockResolvedValue(undefined) }));

import { saveConnection, testConnection } from './backendSwitch.svelte';
import { configStore, currentIdentityScope } from './config.svelte';
import { serverSettingsStore } from './serverSettings.svelte';
import { threadsStore } from './threads.svelte';
import { chatStore } from './chat.svelte';
import { autonomousStore } from './autonomous.svelte';
import { healthStore } from './health.svelte';
import { notificationStore } from './notifications.svelte';
import { backupToPreferences } from '$lib/utils/lifecycle';
import { api } from '$lib/services/api.svelte';

const A = env.A;
const B = 'http://localhost:8098';

function threadRow(id: string) {
  return {
    thread_id: id,
    title: id,
    pinned: false,
    platform: 'mobile',
    platform_meta: null,
    created_at: null,
    updated_at: null,
    title_source: 'user',
  };
}

function settings(model: string) {
  return {
    llm_provider: 'anthropic',
    llm_provider_route: 'default',
    llm_model: model,
    llm_fast_model_resolved: null,
    llm_smart_model_resolved: null,
    memory_char_limit: null,
  };
}

/** Back on A, A's settings loaded, A's thread open, mocks clean. */
async function settleOnA() {
  env.me[A] = 'default';
  env.me[B] = 'default';
  if (configStore.apiUrl !== A || configStore.apiKey !== 'nym_a') await saveConnection(A, 'nym_a');
  (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('a-thread')] });
  threadsStore.reset();
  await threadsStore.syncFromBackend();
  threadsStore.selectThread('a-thread');
  (api.getServerSettings as Mock).mockResolvedValueOnce(settings('model-a'));
  await serverSettingsStore.load();
  expect(serverSettingsStore.model).toBe('model-a');
  expect(threadsStore.currentThreadId).toBe('a-thread');
  vi.clearAllMocks();
  env.fetchCalls.length = 0;
}

beforeEach(async () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  await settleOnA();
});

describe('Save with a changed connection runs the full switch', () => {
  it('tears down on A, repoints, resets every scoped store, resyncs from B, reconnects', async () => {
    let urlAtDisconnect = '';
    let urlAtConnect = '';
    (autonomousStore.disconnect as Mock).mockImplementation(() => {
      urlAtDisconnect = configStore.apiUrl;
    });
    (autonomousStore.connect as Mock).mockImplementation(() => {
      urlAtConnect = configStore.apiUrl;
    });
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('b-thread')] });

    expect(await saveConnection(`${B}/`, ' nym_b ')).toBe(true);

    // Torn down while config still named A; reconnected once it names B.
    expect(urlAtDisconnect).toBe(A);
    expect(healthStore.stopPolling).toHaveBeenCalledTimes(1);
    expect(notificationStore.stopPolling).toHaveBeenCalledTimes(1);
    expect(chatStore.clearMessages).toHaveBeenCalledTimes(1);
    expect(urlAtConnect).toBe(B);
    expect(healthStore.startPolling).toHaveBeenCalledTimes(1);
    expect(notificationStore.startPolling).toHaveBeenCalledTimes(1);

    // Repointed (trimmed), identity re-resolved on B, B's scope live.
    expect(configStore.apiUrl).toBe(B);
    expect(configStore.apiKey).toBe('nym_b');
    expect(env.fetchCalls).toContain(`${B}/me`);
    expect(currentIdentityScope()?.backend).toBe(B);

    // A's state is gone even though both owners are `default`.
    expect(serverSettingsStore.model).toBeNull();
    expect(serverSettingsStore.loaded).toBe(false);
    expect(threadsStore.currentThreadId).toBeNull();
    expect(threadsStore.threads.map((t) => t.id)).toEqual(['b-thread']);

    // The new config is backed up to Preferences right away.
    expect(backupToPreferences).toHaveBeenCalledTimes(1);
  });

  it('a token change on the same URL is a switch too', async () => {
    expect(await saveConnection(A, 'nym_a_rotated')).toBe(true);
    expect(autonomousStore.disconnect).toHaveBeenCalledTimes(1);
    expect(serverSettingsStore.model).toBeNull();
    expect(threadsStore.currentThreadId).toBeNull();
  });

  it('a refused token does not reconnect the stream or polling', async () => {
    env.me[B] = '401';
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [] });
    await saveConnection(B, 'nym_bad');

    expect(configStore.isConfigured).toBe(false);
    expect(autonomousStore.connect).not.toHaveBeenCalled();
    expect(healthStore.startPolling).not.toHaveBeenCalled();
    expect(serverSettingsStore.model).toBeNull();
  });
});

describe('Save with the live values', () => {
  it('tears nothing down and keeps every store (URL spelling differences included)', async () => {
    configStore.setupCompleted = false;
    expect(await saveConnection(`  ${A}/ `, 'nym_a')).toBe(false);

    expect(configStore.setupCompleted).toBe(true);
    expect(autonomousStore.disconnect).not.toHaveBeenCalled();
    expect(chatStore.clearMessages).not.toHaveBeenCalled();
    expect(env.fetchCalls).toEqual([]);
    expect(serverSettingsStore.model).toBe('model-a');
    expect(threadsStore.currentThreadId).toBe('a-thread');
  });
});

describe('Test validates the form and never touches the live connection', () => {
  it('a failed Test leaves the live config and its stored copy exactly as they were', async () => {
    const storedBefore = env.storage.getItem('nymeria-config');
    const result = await testConnection('http://localhost:8099', 'nym_c');

    expect(result.ok).toBe(false);
    expect(result.message).toMatch(/Cannot reach this server/);
    expect(configStore.apiUrl).toBe(A);
    expect(configStore.apiKey).toBe('nym_a');
    expect(env.storage.getItem('nymeria-config')).toBe(storedBefore);
    expect(autonomousStore.disconnect).not.toHaveBeenCalled();
    expect(serverSettingsStore.model).toBe('model-a');
  });

  it('a passing Test probes the FORM backend with the FORM token and still changes nothing', async () => {
    const storedBefore = env.storage.getItem('nymeria-config');
    const result = await testConnection(` ${B} `, ' nym_b ');

    expect(result).toEqual({ ok: true, message: 'Connection successful!' });
    expect(env.fetchCalls).toEqual(expect.arrayContaining([`${B}/health`, `${B}/me`]));
    expect(env.fetchCalls.every((url) => url.startsWith(`${B}/`))).toBe(true);
    expect(configStore.apiUrl).toBe(A);
    expect(configStore.apiKey).toBe('nym_a');
    expect(env.storage.getItem('nymeria-config')).toBe(storedBefore);
    expect(currentIdentityScope()?.backend).toBe(A);
  });
});
