import { describe, expect, it, vi, beforeEach, type Mock } from 'vitest';

// #381: a `user`-role login must not loop GET /settings -> 403 with a toast
// per failure. The store latches, and a known non-admin never asks.
const hooks = vi.hoisted(() => ({
  reload: null as (() => void) | null,
  identity: null as { role?: string } | null,
}));
vi.mock('./config.svelte', () => ({
  configStore: {
    get identity() {
      return hooks.identity;
    },
  },
  registerIdentityReloadHook: (hook: () => void) => {
    hooks.reload = hook;
    return () => undefined;
  },
}));
vi.mock('$lib/services/api.svelte', () => ({
  api: { getServerSettings: vi.fn() },
}));
vi.mock('./errors.svelte', () => ({
  errorsStore: { push: vi.fn() },
}));
vi.mock('$lib/services/api/humanizeError', () => ({
  humanizeErrorText: (e: unknown) => `humanized: ${(e as Error).message}`,
}));

import { createServerSettingsStore } from './serverSettings.svelte';
import { api } from '$lib/services/api.svelte';
import { errorsStore } from './errors.svelte';

const SETTINGS = {
  llm_provider: 'anthropic',
  llm_provider_route: 'anthropic_messages',
  llm_model: 'claude-fable-5',
  llm_fast_model_resolved: 'claude-haiku-4-5',
  llm_smart_model_resolved: 'claude-fable-5',
  memory_char_limit: 4000,
};

function statusError(status: number): Error {
  return Object.assign(new Error(`API error: ${status}`), { status });
}

beforeEach(() => {
  vi.clearAllMocks();
  hooks.identity = null;
  hooks.reload = null;
});

describe('serverSettingsStore', () => {
  it('a known non-admin identity never asks: forbidden and settled without a request', async () => {
    hooks.identity = { role: 'user' };
    const store = createServerSettingsStore();
    await store.load();
    await store.load();
    expect(api.getServerSettings).not.toHaveBeenCalled();
    expect(store.forbidden).toBe(true);
    expect(store.settled).toBe(true);
    expect(store.loaded).toBe(false);
    expect(errorsStore.push).not.toHaveBeenCalled();
  });

  it('a 403 with an unknown identity latches forbidden silently: one request, no toast, no retry', async () => {
    (api.getServerSettings as Mock).mockRejectedValue(statusError(403));
    const store = createServerSettingsStore();
    await store.load();
    await store.load();
    await store.load();
    expect(api.getServerSettings).toHaveBeenCalledTimes(1);
    expect(store.forbidden).toBe(true);
    expect(store.error).toBeNull();
    expect(store.settled).toBe(true);
    expect(errorsStore.push).not.toHaveBeenCalled();
  });

  it('any other failure toasts once and latches; refresh() asks again', async () => {
    (api.getServerSettings as Mock).mockRejectedValue(statusError(500));
    const store = createServerSettingsStore();
    await store.load();
    await store.load();
    expect(api.getServerSettings).toHaveBeenCalledTimes(1);
    expect(errorsStore.push).toHaveBeenCalledTimes(1);
    expect(errorsStore.push).toHaveBeenCalledWith({
      kind: 'generic',
      message: 'humanized: API error: 500',
    });
    expect(store.error).toBe('humanized: API error: 500');
    expect(store.forbidden).toBe(false);
    expect(store.settled).toBe(true);

    (api.getServerSettings as Mock).mockResolvedValue(SETTINGS);
    await store.refresh();
    expect(api.getServerSettings).toHaveBeenCalledTimes(2);
    expect(store.error).toBeNull();
    expect(store.loaded).toBe(true);
    expect(store.model).toBe('claude-fable-5');
  });

  it('an identity switch clears the latches so the new account is asked', async () => {
    (api.getServerSettings as Mock).mockRejectedValue(statusError(403));
    const store = createServerSettingsStore();
    await store.load();
    expect(store.forbidden).toBe(true);

    (api.getServerSettings as Mock).mockResolvedValue(SETTINGS);
    hooks.identity = { role: 'admin' };
    expect(hooks.reload).not.toBeNull();
    hooks.reload!();
    expect(store.settled).toBe(false);
    await store.load();
    expect(api.getServerSettings).toHaveBeenCalledTimes(2);
    expect(store.forbidden).toBe(false);
    expect(store.loaded).toBe(true);
    expect(store.provider).toBe('anthropic');
    expect(store.memoryCharLimit).toBe(4000);
  });

  it('a role promotion on the same account un-settles the store without a reload hook', async () => {
    hooks.identity = { role: 'user' };
    (api.getServerSettings as Mock).mockResolvedValue(SETTINGS);
    const store = createServerSettingsStore();
    await store.load();
    expect(api.getServerSettings).not.toHaveBeenCalled();
    expect(store.settled).toBe(true);

    hooks.identity = { role: 'admin' };
    expect(store.forbidden).toBe(false);
    expect(store.settled).toBe(false);
    await store.load();
    expect(api.getServerSettings).toHaveBeenCalledTimes(1);
    expect(store.loaded).toBe(true);
    expect(store.model).toBe('claude-fable-5');
  });

  it('refresh() during an in-flight load does not start a second request', async () => {
    let settle: (value: typeof SETTINGS) => void = () => undefined;
    (api.getServerSettings as Mock).mockImplementation(
      () => new Promise<typeof SETTINGS>((resolve) => { settle = resolve; }),
    );
    const store = createServerSettingsStore();
    const first = store.load();
    expect(store.loading).toBe(true);
    await store.refresh();
    expect(api.getServerSettings).toHaveBeenCalledTimes(1);
    settle(SETTINGS);
    await first;
    expect(store.loaded).toBe(true);
    expect(store.loading).toBe(false);
    expect(store.provider).toBe('anthropic');
  });

  it('an admin loads once and does not reload while loaded', async () => {
    hooks.identity = { role: 'admin' };
    (api.getServerSettings as Mock).mockResolvedValue(SETTINGS);
    const store = createServerSettingsStore();
    await store.load();
    await store.load();
    expect(api.getServerSettings).toHaveBeenCalledTimes(1);
    expect(store.loaded).toBe(true);
    expect(store.settled).toBe(true);
    expect(store.fastModelResolved).toBe('claude-haiku-4-5');
    expect(errorsStore.push).not.toHaveBeenCalled();
  });
});
