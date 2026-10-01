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
  // `gate.hold`, when set, keeps every /me in flight until it resolves.
  const me: Record<string, string> = {};
  const gate = { hold: null as Promise<void> | null };
  (globalThis as { fetch?: unknown }).fetch = async (url: string) => {
    if (gate.hold) await gate.hold;
    const base = Object.keys(me).find((b) => url.startsWith(`${b}/`));
    const answer = base ? me[base] : 'down';
    if (answer === 'down') throw new TypeError('Failed to fetch');
    return new Response(
      JSON.stringify({ id: answer, email: `${answer}@localhost`, display_name: answer, role: 'admin' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  };
  return { storage, me, gate, A };
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
    getThreadHistory: vi.fn(),
    getThreadContextStats: vi.fn(),
    getThreadStatus: vi.fn(),
    submitUiPromptResult: vi.fn(),
    cancelCredentialPrompt: vi.fn(),
    endBrowserLoginSession: vi.fn(),
  },
  hasActiveStreamForThread: () => false,
}));
vi.mock('$lib/stores/chat.svelte', () => ({
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
vi.mock('$lib/stores/autonomous.svelte', () => ({
  autonomousStore: { disconnect: vi.fn(), connect: vi.fn() },
}));
vi.mock('$lib/stores/notifications.svelte', () => ({
  notificationStore: { stopPolling: vi.fn(), startPolling: vi.fn() },
}));
vi.mock('$lib/stores/syncPoll.svelte', () => ({ stopSyncPoll: vi.fn(), startSyncPoll: vi.fn() }));

import { createConnectionsStore } from './connections.svelte';
import { configStore, currentIdentityScope, scopedKey } from './config.svelte';
import { serverSettingsStore } from './serverSettings.svelte';
import { threadsStore } from './threads.svelte';
import { threadConfigStore } from './threadConfig.svelte';
import { uiPromptStore } from './uiPrompt.svelte';
import { authPromptStore } from './authPrompt.svelte';
import { browserLoginStore } from './browserLogin.svelte';
import { api } from '$lib/services/api.svelte';
import type { AuthPromptEvent, UiPromptEvent } from '$lib/types';
import type { BrowserLoginSessionStatus } from '$lib/services/api/browser-login';

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
  (api.getThreadHistory as Mock).mockResolvedValue({ messages: [] });
  (api.getThreadContextStats as Mock).mockResolvedValue(null);
  (api.getThreadStatus as Mock).mockResolvedValue({});
  await settleOnA();
  vi.clearAllMocks();
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

  it('when B`s /me fails with a network error the switch lands empty, parked on B`s own keys, never on A`s values', async () => {
    env.me[B] = 'down';
    await connections.applyConnection(B, 'nym_b');

    expect(configStore.apiUrl).toBe(B);
    expect(configStore.identity).toBeNull();
    // Provisional: backend known, account not yet (review S-LOW-4). Writes in
    // this state stay B's; they used to land on the unscoped keys, which the
    // next backend to resolve adopted.
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: '' });
    expect(scopedKey('nymeria-thread-folders')).toBe(`nymeria-thread-folders-@${B}`);
    expect(serverSettingsStore.model).toBeNull();
    expect(serverSettingsStore.loaded).toBe(false);
    expect(threadConfigStore.getConfig(THREAD)).toBeUndefined();
  });
});

describe('the reset runs BEFORE the config names B (review S-MED-1)', () => {
  it('while B`s /me is in flight nothing A served is readable, so no click can post it to B', async () => {
    threadsStore.ensureThread('a-only', 'Only on A');
    threadsStore.selectThread(THREAD);
    let release: () => void = () => undefined;
    env.gate.hold = new Promise<void>((resolve) => {
      release = resolve;
    });
    const switching = connections.applyConnection(B, 'nym_b');
    try {
      // The api client already targets B and /me has not answered.
      expect(configStore.apiUrl).toBe(B);
      expect(configStore.identity).toBeNull();
      expect(serverSettingsStore.model).toBeNull();
      expect(serverSettingsStore.fastModelResolved).toBeNull();
      expect(threadConfigStore.getConfig(THREAD)).toBeUndefined();
      expect(threadsStore.currentThreadId).toBeNull();
      expect(threadsStore.threads.some((t) => t.id === 'a-only')).toBe(false);
    } finally {
      env.gate.hold = null;
      release();
      await switching;
    }
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: 'default' });
  });
});

describe('thread task state is per backend (review S-MED-3)', () => {
  it('a task spinner and count from A do not survive on B`s row with the same thread id', async () => {
    threadsStore.setThreadActive(THREAD, true);
    threadsStore.setThreadTaskCounts({ [THREAD]: 3 });

    await connections.applyConnection(B, 'nym_b');

    expect(threadsStore.threads.map((t) => t.id)).toContain(THREAD);
    expect(threadsStore.isThreadActive(THREAD)).toBe(false);
    expect(threadsStore.getThreadTaskCount(THREAD)).toBe(0);
  });

  it('the reload hook alone clears it too (an account change on the same backend)', async () => {
    threadsStore.setThreadActive(THREAD, true);
    threadsStore.setThreadTaskCounts({ [THREAD]: 3 });
    env.me[A] = 'alice';

    await configStore.refreshIdentity();

    expect(configStore.identity?.id).toBe('alice');
    expect(threadsStore.isThreadActive(THREAD)).toBe(false);
    expect(threadsStore.getThreadTaskCount(THREAD)).toBe(0);
  });

  it('reset() clears it on its own', () => {
    threadsStore.setThreadActive(THREAD, true);
    threadsStore.setThreadTaskCounts({ [THREAD]: 3 });
    threadsStore.reset();
    expect(threadsStore.isThreadActive(THREAD)).toBe(false);
    expect(threadsStore.getThreadTaskCount(THREAD)).toBe(0);
  });
});

describe('local-only thread folders are per backend', () => {
  it('a folder made on A is absent on B and back again on A', async () => {
    try {
      threadsStore.createFolder('Work on A');
      expect(threadsStore.folders.map((f) => f.name)).toEqual(['Work on A']);

      await connections.applyConnection(B, 'nym_b');
      expect(threadsStore.folders).toEqual([]);
      threadsStore.createFolder('Home on B');

      await connections.applyConnection(A, 'nym_a');
      expect(threadsStore.folders.map((f) => f.name)).toEqual(['Work on A']);

      await connections.applyConnection(B, 'nym_b');
      expect(threadsStore.folders.map((f) => f.name)).toEqual(['Home on B']);
    } finally {
      // The stores are module-shared: a failure above must not leak folders
      // into the next test's baseline on either backend.
      env.me[B] = 'default';
      await connections.applyConnection(B, 'nym_b');
      for (const f of threadsStore.folders) threadsStore.deleteFolder(f.id);
      await connections.applyConnection(A, 'nym_a');
      for (const f of threadsStore.folders) threadsStore.deleteFolder(f.id);
    }
  });
});

describe('open prompt modals are cancelled on the backend that opened them (review C-LOW-2)', () => {
  /** Each cancel as `what@url/token`, the url and token read when the call is made (as the api client does). */
  function recordCancels(): string[] {
    const seen: string[] = [];
    const where = () => `@${configStore.apiUrl}/${configStore.apiKey}`;
    (api.submitUiPromptResult as Mock).mockImplementation(async (id: string, req: { status: string }) => {
      seen.push(`ui:${id}:${req.status}${where()}`);
      return { delivered: true };
    });
    (api.cancelCredentialPrompt as Mock).mockImplementation(async (id: string) => {
      seen.push(`credential:${id}${where()}`);
    });
    (api.endBrowserLoginSession as Mock).mockImplementation(async (id: string, reason: string) => {
      seen.push(`login:${id}:${reason}${where()}`);
      return {};
    });
    return seen;
  }

  it('each open modal is cancelled on A with A`s token, before the repoint, and closed; nothing reaches B', async () => {
    const seen = recordCancels();
    uiPromptStore.open({ prompt_id: 'ui-1', thread_id: 't', title: '', html: '<p/>', timeout_seconds: 60, expires_at: null } as UiPromptEvent);
    authPromptStore.open({ prompt_id: 'cred-1' } as unknown as AuthPromptEvent);
    browserLoginStore.open({ session_id: 'bl-1', state: 'active', seconds_remaining: 600 } as BrowserLoginSessionStatus, 'agent');

    await connections.applyConnection(B, 'nym_b');

    expect(seen).toEqual([
      `ui:ui-1:cancelled@${A}/nym_a`,
      `credential:cred-1@${A}/nym_a`,
      `login:bl-1:cancelled@${A}/nym_a`,
    ]);
    expect(uiPromptStore.active).toBeNull();
    expect(authPromptStore.active).toBeNull();
    expect(browserLoginStore.active).toBeNull();
  });

  it('a credential prompt that already resolved and a login that already ended close without a cancel', async () => {
    const seen = recordCancels();
    authPromptStore.open({ prompt_id: 'cred-2', mode: 'oauth' } as unknown as AuthPromptEvent);
    authPromptStore.resolveById('cred-2', { ok: true, status: 'connected' });
    browserLoginStore.open({ session_id: 'bl-2', state: 'active', seconds_remaining: 600 } as BrowserLoginSessionStatus, 'agent');
    browserLoginStore.endById('bl-2', 'completed');

    await connections.applyConnection(B, 'nym_b');

    expect(seen).toEqual([]);
    expect(authPromptStore.active).toBeNull();
    expect(browserLoginStore.active).toBeNull();
  });
});

describe('a switch reopens the backend`s last open thread when it still exists there (review C-LOW-1)', () => {
  const onA = { threads: [threadRow('desk-a', 'Desk on A')] };
  const onB = { threads: [threadRow('desk-b', 'Desk on B')] };

  it('back on A, A`s open thread reopens with its history; a first visit to B opens nothing', async () => {
    (api.listThreadsWithMetadata as Mock).mockResolvedValue(onA);
    await connections.applyConnection(A, 'nym_a');
    threadsStore.selectThread('desk-a');

    (api.listThreadsWithMetadata as Mock).mockResolvedValue(onB);
    await connections.applyConnection(B, 'nym_b');
    expect(threadsStore.currentThreadId).toBeNull();
    expect(api.getThreadHistory).not.toHaveBeenCalled();

    (api.listThreadsWithMetadata as Mock).mockResolvedValue(onA);
    await connections.applyConnection(A, 'nym_a');
    expect(threadsStore.currentThreadId).toBe('desk-a');
    expect(api.getThreadHistory).toHaveBeenCalledWith('desk-a');
  });

  it('a thread the backend no longer has is not reopened', async () => {
    (api.listThreadsWithMetadata as Mock).mockResolvedValue(onA);
    await connections.applyConnection(A, 'nym_a');
    threadsStore.selectThread('desk-a');
    (api.listThreadsWithMetadata as Mock).mockResolvedValue(onB);
    await connections.applyConnection(B, 'nym_b');

    (api.getThreadHistory as Mock).mockClear();
    (api.listThreadsWithMetadata as Mock).mockResolvedValue({ threads: [threadRow('desk-a2', 'Another')] });
    await connections.applyConnection(A, 'nym_a');
    expect(threadsStore.currentThreadId).toBeNull();
    expect(api.getThreadHistory).not.toHaveBeenCalled();
  });
});
