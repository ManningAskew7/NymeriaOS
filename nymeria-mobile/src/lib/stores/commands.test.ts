import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

// Capture the identity-reload hook and stub the api singleton so the heavy
// api/index.ts chain (and the real config store) stay out of the test.
const { listCommandsMock, identityHooks } = vi.hoisted(() => ({
  listCommandsMock: vi.fn(),
  identityHooks: [] as Array<() => void>,
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: { listCommands: listCommandsMock },
}));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (fn: () => void) => {
    identityHooks.push(fn);
  },
}));

import { createCommandsStore } from './commands.svelte';
import type { SlashCommandInfo } from '$lib/types';

function cmd(
  name: string,
  execution_kind: SlashCommandInfo['execution_kind'] = 'command',
  path = name.split(' ')
): SlashCommandInfo {
  return {
    name,
    description: `${name} description`,
    usage: `/${name}`,
    category: 'Test',
    subcommands: [],
    id: name.replace(/\s+/g, '.'),
    path,
    aliases: [],
    scope: 'global',
    surfaces: [],
    agent_allowed: true,
    requires_thread: false,
    requires_admin: false,
    mutates_state: false,
    danger_level: 'safe',
    execution_kind,
  };
}

describe('commandsStore: one shared catalog cache (backlog #135)', () => {
  let store: ReturnType<typeof createCommandsStore>;

  beforeEach(() => {
    listCommandsMock.mockReset();
    identityHooks.length = 0;
    store = createCommandsStore();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('serves concurrent palette and routing consumers from ONE fetch', async () => {
    listCommandsMock.mockResolvedValue([cmd('compact', 'chat_stream'), cmd('model')]);

    const [, roots] = await Promise.all([store.ensureLoaded(), store.chatStreamRoots()]);

    expect(listCommandsMock).toHaveBeenCalledTimes(1);
    expect(store.commands.map((c) => c.name)).toEqual(['compact', 'model']);
    expect(store.loaded).toBe(true);
    expect(roots.has('/compact')).toBe(true);
  });

  it('derives chat-stream roots from execution_kind and the ROOT path segment', async () => {
    listCommandsMock.mockResolvedValue([
      cmd('compact', 'chat_stream'),
      cmd('goal start', 'chat_stream'), // multi-segment path: root only
      cmd('model'), // plain command: excluded
    ]);

    const roots = await store.chatStreamRoots();

    expect([...roots].sort()).toEqual(['/compact', '/goal']);
  });

  it('degrades to the static fallback roots on a failed fetch, without a refetch per call', async () => {
    listCommandsMock.mockRejectedValue(new TypeError('Failed to fetch'));

    const roots = await store.chatStreamRoots();

    // The static set covers the live chat_stream catalog.
    expect(roots.has('/compact')).toBe(true);
    expect(roots.has('/resume')).toBe(true);
    expect(store.commands).toEqual([]);

    // Within the retry window every consumer call is a no-op, not a request
    // per keystroke against a down backend.
    await store.chatStreamRoots();
    await store.ensureLoaded();
    expect(listCommandsMock).toHaveBeenCalledTimes(1);
  });

  it('retries after the backoff window and recovers the catalog', async () => {
    const t0 = 1_000_000;
    const now = vi.spyOn(Date, 'now').mockReturnValue(t0);
    listCommandsMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await store.ensureLoaded();
    expect(listCommandsMock).toHaveBeenCalledTimes(1);

    // Backend comes back; past the window the next consumer retries.
    now.mockReturnValue(t0 + 16_000);
    listCommandsMock.mockResolvedValue([cmd('compact', 'chat_stream')]);
    const roots = await store.chatStreamRoots();

    expect(listCommandsMock).toHaveBeenCalledTimes(2);
    expect(store.loaded).toBe(true);
    expect(roots.has('/compact')).toBe(true);
  });

  it('resets on identity reload and refetches for the new account', async () => {
    listCommandsMock.mockResolvedValue([cmd('model')]);
    await store.ensureLoaded();
    expect(store.commands).toHaveLength(1);

    identityHooks.forEach((fn) => fn());
    expect(store.commands).toEqual([]);
    expect(store.loaded).toBe(false);

    listCommandsMock.mockResolvedValue([cmd('think')]);
    await store.ensureLoaded();
    expect(listCommandsMock).toHaveBeenCalledTimes(2);
    expect(store.commands.map((c) => c.name)).toEqual(['think']);
  });

  it('the routing await never hangs a send on a stuck catalog fetch', async () => {
    vi.useFakeTimers();
    try {
      // A HUNG backend: the fetch neither resolves nor rejects.
      listCommandsMock.mockReturnValue(new Promise<SlashCommandInfo[]>(() => {}));

      const rootsPromise = store.chatStreamRoots();
      await vi.advanceTimersByTimeAsync(3_100);
      const roots = await rootsPromise;

      // Past the deadline the send routes with the fallback set.
      expect(roots.has('/compact')).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('discards an in-flight fetch that resolves after an identity reload', async () => {
    let resolveFetch!: (v: SlashCommandInfo[]) => void;
    listCommandsMock.mockReturnValue(
      new Promise<SlashCommandInfo[]>((resolve) => {
        resolveFetch = resolve;
      })
    );

    const pending = store.ensureLoaded();
    identityHooks.forEach((fn) => fn());
    resolveFetch([cmd('model')]);
    await pending;

    // The stale response must not leak into the new account's cache.
    expect(store.commands).toEqual([]);
    expect(store.loaded).toBe(false);
  });
});
