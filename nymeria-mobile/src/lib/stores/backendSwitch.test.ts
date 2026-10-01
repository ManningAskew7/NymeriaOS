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
  // `gate.hold`, when set, keeps every request in flight until it resolves.
  const me: Record<string, string> = {};
  const fetchCalls: string[] = [];
  const gate = { hold: null as Promise<void> | null };
  (globalThis as { fetch?: unknown }).fetch = async (url: string) => {
    fetchCalls.push(url);
    if (gate.hold) await gate.hold;
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
  return { storage, me, fetchCalls, gate, A };
});

vi.mock('$lib/themes', () => ({ applyTheme: vi.fn() }));
vi.mock('$lib/services/api.svelte', async () => {
  const base = await vi.importActual<typeof import('$lib/services/api/base')>('$lib/services/api/base');
  return {
    probeConnection: base.probeConnection,
    hasActiveStreamForThread: () => false,
    api: {
      getServerSettings: vi.fn(),
      listThreadsWithMetadata: vi.fn(),
      getThreadHistory: vi.fn(),
      getThreadContextStats: vi.fn(),
      getThreadStatus: vi.fn(),
    },
  };
});
vi.mock('./chat.svelte', () => ({
  chatStore: {
    messages: [],
    clearMessages: vi.fn(),
    prepareForThreadSwitch: vi.fn(),
    setLoadingHistory: vi.fn(),
    setMessages: vi.fn(),
    setContextStats: vi.fn(),
    setActiveModel: vi.fn(),
  },
}));
vi.mock('./ui.svelte', () => ({ uiStore: { goToChat: vi.fn() } }));
vi.mock('./autonomous.svelte', () => ({ autonomousStore: { disconnect: vi.fn(), connect: vi.fn() } }));
vi.mock('./health.svelte', () => ({ healthStore: { stopPolling: vi.fn(), startPolling: vi.fn() } }));
vi.mock('./notifications.svelte', () => ({
  notificationStore: { stopPolling: vi.fn(), startPolling: vi.fn() },
}));
vi.mock('$lib/utils/lifecycle', () => ({ backupToPreferences: vi.fn().mockResolvedValue(undefined) }));

import { saveConnection, saveOutcomeMessage, testConnection, type SaveOutcome } from './backendSwitch.svelte';
import { configStore, currentIdentityScope, scopedKey } from './config.svelte';
import { serverSettingsStore } from './serverSettings.svelte';
import { threadsStore } from './threads.svelte';
import { chatStore } from './chat.svelte';
import { autonomousStore } from './autonomous.svelte';
import { healthStore } from './health.svelte';
import { notificationStore } from './notifications.svelte';
import { backupToPreferences } from '$lib/utils/lifecycle';
import { api } from '$lib/services/api.svelte';
import { uiStore } from './ui.svelte';

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
  (api.getThreadHistory as Mock).mockResolvedValue({ messages: [] });
  (api.getThreadContextStats as Mock).mockResolvedValue(null);
  (api.getThreadStatus as Mock).mockResolvedValue({});
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

    expect(await saveConnection(`${B}/`, ' nym_b ')).toBe('connected');

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

  it('a token change on the same URL is a switch too, and lands back on the open thread', async () => {
    expect(await saveConnection(A, 'nym_a_rotated')).toBe('connected');
    expect(autonomousStore.disconnect).toHaveBeenCalledTimes(1);
    expect(serverSettingsStore.model).toBeNull();
    // Review C-LOW-1: the switch reopens the thread this backend + account
    // last had open when it still exists (it used to land on none).
    expect(threadsStore.currentThreadId).toBe('a-thread');
  });

  it('a refused token does not reconnect the stream or polling, and says so', async () => {
    env.me[B] = '401';
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [] });
    expect(await saveConnection(B, 'nym_bad')).toBe('refused');

    expect(configStore.isConfigured).toBe(false);
    expect(autonomousStore.connect).not.toHaveBeenCalled();
    expect(healthStore.startPolling).not.toHaveBeenCalled();
    expect(serverSettingsStore.model).toBeNull();
  });
});

describe('the reset runs BEFORE the config names B (review S-MED-1)', () => {
  it('while B`s /me is in flight nothing A served is readable, so a send cannot post A`s thread to B', async () => {
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('b-thread')] });
    let release: () => void = () => undefined;
    env.gate.hold = new Promise<void>((resolve) => {
      release = resolve;
    });
    const saving = saveConnection(B, 'nym_b');
    try {
      expect(configStore.apiUrl).toBe(B);
      expect(configStore.identity).toBeNull();
      expect(serverSettingsStore.model).toBeNull();
      expect(threadsStore.currentThreadId).toBeNull();
      expect(threadsStore.threads.some((t) => t.id === 'a-thread')).toBe(false);
    } finally {
      env.gate.hold = null;
      release();
      await saving;
    }
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: 'default' });
  });
});

describe('thread task state is per backend (review S-MED-3)', () => {
  it('a task spinner and count from A do not survive the switch', async () => {
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('a-thread')] });
    threadsStore.setThreadActive('a-thread', true);
    threadsStore.setThreadTaskCounts({ 'a-thread': 2 });

    await saveConnection(B, 'nym_b');

    expect(threadsStore.threads.map((t) => t.id)).toEqual(['a-thread']);
    expect(threadsStore.isThreadActive('a-thread')).toBe(false);
    expect(threadsStore.getThreadTaskCount('a-thread')).toBe(0);
  });

  it('the reload hook alone clears it too (an account change on the same backend)', async () => {
    threadsStore.setThreadActive('a-thread', true);
    threadsStore.setThreadTaskCounts({ 'a-thread': 2 });
    env.me[A] = 'alice';
    try {
      await configStore.refreshIdentity();

      expect(configStore.identity?.id).toBe('alice');
      expect(threadsStore.isThreadActive('a-thread')).toBe(false);
      expect(threadsStore.getThreadTaskCount('a-thread')).toBe(0);
    } finally {
      // settleOnA skips the switch when URL and token match: restore `default`.
      env.me[A] = 'default';
      await configStore.refreshIdentity();
    }
  });

  it('reset() clears it on its own', () => {
    threadsStore.setThreadActive('a-thread', true);
    threadsStore.setThreadTaskCounts({ 'a-thread': 2 });
    threadsStore.reset();
    expect(threadsStore.isThreadActive('a-thread')).toBe(false);
    expect(threadsStore.getThreadTaskCount('a-thread')).toBe(0);
  });
});

describe('the Save message reports how the switch landed (review S-LOW-5)', () => {
  it('a switch whose /me never answers is `unreachable`, never "connected", and stays on B with nothing of A`s', async () => {
    env.me[B] = 'down';
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [] });
    const outcome = await saveConnection(B, 'nym_b');

    expect(outcome).toBe('unreachable');
    expect(saveOutcomeMessage(outcome).ok).toBe(false);
    expect(saveOutcomeMessage(outcome).message).not.toMatch(/connected/i);
    expect(configStore.apiUrl).toBe(B);
    expect(configStore.identity).toBeNull();
    // Parked on B's provisional keys (backend known, account not yet), never
    // A's scope and never the unscoped keys the next backend would adopt.
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: '' });
    expect(scopedKey('nymeria-thread-folders')).toBe(`nymeria-thread-folders-@${B}`);
    expect(serverSettingsStore.model).toBeNull();
  });

  it('a blank URL or token is refused before any teardown, and nothing is written', async () => {
    const storedBefore = env.storage.getItem('nymeria-config');
    expect(await saveConnection('   ', 'nym_b')).toBe('incomplete');
    expect(await saveConnection(B, '  ')).toBe('incomplete');

    expect(autonomousStore.disconnect).not.toHaveBeenCalled();
    expect(chatStore.clearMessages).not.toHaveBeenCalled();
    expect(env.fetchCalls).toEqual([]);
    expect(configStore.apiUrl).toBe(A);
    expect(configStore.apiKey).toBe('nym_a');
    expect(env.storage.getItem('nymeria-config')).toBe(storedBefore);
    expect(serverSettingsStore.model).toBe('model-a');
    expect(threadsStore.currentThreadId).toBe('a-thread');
  });

  it('only a usable connection reads as success', () => {
    const ok = (o: SaveOutcome) => saveOutcomeMessage(o).ok;
    expect(ok('connected')).toBe(true);
    expect(ok('unchanged')).toBe(true);
    expect(ok('unreachable')).toBe(false);
    expect(ok('refused')).toBe(false);
    expect(ok('incomplete')).toBe(false);
  });
});

describe('a switch reopens the backend`s last open thread when it still exists there (review C-LOW-1)', () => {
  it('back on A after B, A`s open thread reopens with its history, without leaving Settings', async () => {
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('b-thread')] });
    await saveConnection(B, 'nym_b');
    expect(threadsStore.currentThreadId).toBeNull();
    expect(api.getThreadHistory).not.toHaveBeenCalled();

    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('a-thread')] });
    await saveConnection(A, 'nym_a');
    expect(threadsStore.currentThreadId).toBe('a-thread');
    expect(api.getThreadHistory).toHaveBeenCalledWith('a-thread');
    expect(uiStore.goToChat).not.toHaveBeenCalled();
  });

  it('a thread the backend no longer has is not reopened', async () => {
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('b-thread')] });
    await saveConnection(B, 'nym_b');
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('a-other')] });
    await saveConnection(A, 'nym_a');
    expect(threadsStore.currentThreadId).toBeNull();
    expect(api.getThreadHistory).not.toHaveBeenCalled();
  });
});

describe('Save with the live values', () => {
  it('tears nothing down and keeps every store (URL spelling differences included)', async () => {
    configStore.setupCompleted = false;
    expect(await saveConnection(`  ${A}/ `, 'nym_a')).toBe('unchanged');

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
