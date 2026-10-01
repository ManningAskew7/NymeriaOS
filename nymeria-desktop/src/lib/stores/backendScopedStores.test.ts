import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

// #242: every store that caches something a backend served registers an
// identity reload hook, and the hooks now fire on every connection switch
// (config.svelte.ts). These are the stores shared byte-for-byte by both apps:
// after a switch nothing the previous backend served is readable, and a
// request that was in flight across the switch lands nowhere. The hooks are
// captured from the real stores' registrations; the api is the network
// boundary. Kept identical in desktop and mobile (drift gate EXACT_MATCH).

const reg = vi.hoisted(() => ({ hooks: [] as Array<() => void> }));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
}));
vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getThreadConfig: vi.fn(),
    updateThreadConfig: vi.fn(),
    listThreadBindings: vi.fn(),
    listCredentials: vi.fn(),
    createCredential: vi.fn(),
    getOpenRouterModels: vi.fn(),
  },
}));
vi.mock('$lib/services/api/humanizeError', () => ({
  humanizeErrorText: (e: unknown) => `humanized: ${(e as Error).message}`,
}));

import { threadConfigStore } from './threadConfig.svelte';
import { chatAppBindingsStore } from './chatAppBindings.svelte';
import { credentialsStore } from './credentials.svelte';
import { modelsStore } from './models.svelte';
import { api } from '$lib/services/api.svelte';

/** What a connection switch does to these stores: fire every registered hook. */
function switchBackend(): void {
  for (const hook of reg.hooks) hook();
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (e: unknown) => void = () => undefined;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// A deterministic platform id: the same id names a different thread on
// another backend, which is what makes a per-thread cache dangerous.
const THREAD = 'telegram_5559876543';
const CONFIG_A = { threadId: THREAD, instructions: 'backend A instructions', llmConfig: { model: 'model-a' } };
const CONFIG_B = { threadId: THREAD, instructions: 'backend B instructions', llmConfig: { model: 'model-b' } };

beforeEach(() => {
  vi.clearAllMocks();
  // Each test starts from a clean slate the same way the app does.
  switchBackend();
});

describe('threadConfigStore across a switch', () => {
  it('drops the previous backend config for a thread id both backends have', async () => {
    (api.getThreadConfig as Mock).mockResolvedValueOnce(CONFIG_A);
    await threadConfigStore.loadConfig(THREAD);
    expect(threadConfigStore.getConfig(THREAD)?.instructions).toBe('backend A instructions');

    switchBackend();
    expect(threadConfigStore.getConfig(THREAD)).toBeUndefined();
    expect(threadConfigStore.configs.size).toBe(0);
  });

  it('a load in flight across the switch never repopulates the cache', async () => {
    const stale = deferred<typeof CONFIG_A>();
    (api.getThreadConfig as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = threadConfigStore.loadConfig(THREAD);
    expect(threadConfigStore.isLoading(THREAD)).toBe(true);

    switchBackend();
    expect(threadConfigStore.isLoading(THREAD)).toBe(false);
    stale.resolve(CONFIG_A);
    await inFlight;
    expect(threadConfigStore.getConfig(THREAD)).toBeUndefined();

    (api.getThreadConfig as Mock).mockResolvedValueOnce(CONFIG_B);
    await threadConfigStore.loadConfig(THREAD);
    expect(threadConfigStore.getConfig(THREAD)?.instructions).toBe('backend B instructions');
  });

  it('a save that resolves after the switch does not land in the new cache', async () => {
    const stale = deferred<typeof CONFIG_A>();
    (api.updateThreadConfig as Mock).mockReturnValueOnce(stale.promise);
    const saving = threadConfigStore.updateConfig(THREAD, { instructions: 'x' });
    switchBackend();
    stale.resolve(CONFIG_A);
    await saving;
    expect(threadConfigStore.getConfig(THREAD)).toBeUndefined();
  });
});

describe('chatAppBindingsStore across a switch', () => {
  const BINDING = { id: 1, platform: 'telegram', chat_id: '42', thread_id: THREAD };

  it('drops the previous backend bindings', async () => {
    (api.listThreadBindings as Mock).mockResolvedValueOnce([BINDING]);
    await chatAppBindingsStore.loadBindings(THREAD);
    expect(chatAppBindingsStore.getBindings(THREAD)).toHaveLength(1);

    switchBackend();
    expect(chatAppBindingsStore.getBindings(THREAD)).toEqual([]);
  });

  it('a load in flight across the switch never repopulates', async () => {
    const stale = deferred<(typeof BINDING)[]>();
    (api.listThreadBindings as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = chatAppBindingsStore.loadBindings(THREAD);
    switchBackend();
    expect(chatAppBindingsStore.isLoading(THREAD)).toBe(false);
    stale.resolve([BINDING]);
    await inFlight;
    expect(chatAppBindingsStore.getBindings(THREAD)).toEqual([]);
  });
});

describe('credentialsStore across a switch', () => {
  const CRED = { id: 'cred-1', name: 'github', status: 'active' };

  it('drops the list and re-arms the load gate', async () => {
    (api.listCredentials as Mock).mockResolvedValueOnce({ credentials: [CRED] });
    await credentialsStore.load();
    expect(credentialsStore.credentials).toHaveLength(1);
    expect(credentialsStore.loaded).toBe(true);

    switchBackend();
    expect(credentialsStore.credentials).toEqual([]);
    expect(credentialsStore.loaded).toBe(false);
    expect(credentialsStore.loading).toBe(false);
    expect(credentialsStore.error).toBeNull();
  });

  it('a load in flight across the switch lands nothing, and the new load is not blocked', async () => {
    const stale = deferred<{ credentials: (typeof CRED)[] }>();
    (api.listCredentials as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = credentialsStore.load();
    switchBackend();

    (api.listCredentials as Mock).mockResolvedValueOnce({ credentials: [] });
    await credentialsStore.load();
    expect(api.listCredentials).toHaveBeenCalledTimes(2);

    stale.resolve({ credentials: [CRED] });
    await inFlight;
    expect(credentialsStore.credentials).toEqual([]);
    expect(credentialsStore.loaded).toBe(true);
  });

  it('a create that resolves after the switch is not added to the new list', async () => {
    const stale = deferred<typeof CRED>();
    (api.createCredential as Mock).mockReturnValueOnce(stale.promise);
    const creating = credentialsStore.create({ name: 'github' } as never);
    switchBackend();
    stale.resolve(CRED);
    expect(await creating).toBeNull();
    expect(credentialsStore.credentials).toEqual([]);
  });

  it('a failed load settles with the error shown, so the panel effect cannot re-drive it', async () => {
    (api.listCredentials as Mock).mockRejectedValueOnce(new Error('API error: 500'));
    await credentialsStore.load();
    expect(credentialsStore.loaded).toBe(true);
    expect(credentialsStore.loading).toBe(false);
    expect(credentialsStore.error).toBe('humanized: API error: 500');

    (api.listCredentials as Mock).mockResolvedValueOnce({ credentials: [CRED] });
    await credentialsStore.refresh();
    expect(credentialsStore.error).toBeNull();
    expect(credentialsStore.credentials).toHaveLength(1);
  });
});

describe('modelsStore across a switch', () => {
  const MODEL_A = { id: 'vendor/model-a', name: 'Model A', context_length: 200000 };

  it('drops the catalog and re-arms the load gate', async () => {
    (api.getOpenRouterModels as Mock).mockResolvedValueOnce([MODEL_A]);
    await modelsStore.loadModels();
    expect(modelsStore.getById('vendor/model-a')?.context_length).toBe(200000);

    switchBackend();
    expect(modelsStore.models).toEqual([]);
    expect(modelsStore.getById('vendor/model-a')).toBeUndefined();
    expect(modelsStore.loaded).toBe(false);
  });

  it('a load in flight across the switch lands nothing', async () => {
    const stale = deferred<(typeof MODEL_A)[]>();
    (api.getOpenRouterModels as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = modelsStore.loadModels();
    switchBackend();
    expect(modelsStore.loading).toBe(false);
    stale.resolve([MODEL_A]);
    await inFlight;
    expect(modelsStore.getById('vendor/model-a')).toBeUndefined();
    expect(modelsStore.loaded).toBe(false);
  });
});
