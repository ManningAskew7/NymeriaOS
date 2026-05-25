import { describe, expect, it, vi, beforeEach, type Mock } from 'vitest';

// The connections store orchestrates several sibling stores. Mock them so we
// can assert the backend-switch sequence (config write -> reset -> resync)
// without standing up the real stores, mirroring navigation.test.ts.
vi.mock('$lib/stores/config.svelte', () => ({
  configStore: {
    apiUrl: '',
    apiKey: '',
    identity: null,
    refreshIdentity: vi.fn().mockResolvedValue(null),
  },
}));
vi.mock('$lib/stores/chat.svelte', () => ({
  chatStore: { clearMessages: vi.fn() },
}));
vi.mock('$lib/stores/threads.svelte', () => ({
  threadsStore: { reset: vi.fn(), syncFromBackend: vi.fn().mockResolvedValue(undefined) },
}));
vi.mock('$lib/stores/autonomous.svelte', () => ({
  autonomousStore: { disconnect: vi.fn(), connect: vi.fn() },
}));
vi.mock('$lib/stores/notifications.svelte', () => ({
  notificationStore: { stopPolling: vi.fn(), startPolling: vi.fn() },
}));
vi.mock('$lib/stores/syncPoll.svelte', () => ({
  stopSyncPoll: vi.fn(),
}));

import { createConnectionsStore } from './connections.svelte';
import { configStore } from '$lib/stores/config.svelte';
import { threadsStore } from '$lib/stores/threads.svelte';

const WORK_URL = 'https://nymeria.example.com';
const WORK_KEY = 'nym_work_token';

beforeEach(() => {
  vi.clearAllMocks();
  configStore.apiUrl = '';
  configStore.apiKey = '';
  (configStore.refreshIdentity as Mock).mockResolvedValue(null);
  (threadsStore.syncFromBackend as Mock).mockResolvedValue(undefined);
});

describe('connectionsStore.applyConnection', () => {
  it('writes config, then resets threads, then resyncs — in that order', async () => {
    const store = createConnectionsStore();

    let urlAtReset: string | null = null;
    (threadsStore.reset as Mock).mockImplementation(() => {
      // Config must already point at the new backend when threads are cleared.
      urlAtReset = configStore.apiUrl;
    });

    await store.applyConnection(WORK_URL, WORK_KEY);

    expect(configStore.apiUrl).toBe(WORK_URL);
    expect(configStore.apiKey).toBe(WORK_KEY);
    expect(urlAtReset).toBe(WORK_URL);
    expect(threadsStore.reset).toHaveBeenCalledTimes(1);
    expect(threadsStore.syncFromBackend).toHaveBeenCalledTimes(1);
    expect((threadsStore.reset as Mock).mock.invocationCallOrder[0]).toBeLessThan(
      (threadsStore.syncFromBackend as Mock).mock.invocationCallOrder[0],
    );
  });

  it('pins a matching saved connection as active', async () => {
    const store = createConnectionsStore();
    const saved = store.add('Work', WORK_URL, WORK_KEY);

    await store.applyConnection(WORK_URL, WORK_KEY);

    expect(store.activeConnectionId).toBe(saved.id);
  });

  it('clears the active connection when no saved entry matches', async () => {
    const store = createConnectionsStore();
    store.add('Work', WORK_URL, WORK_KEY);

    await store.applyConnection('https://other.example.com', 'nym_other');

    expect(store.activeConnectionId).toBeNull();
  });
});

describe('connectionsStore.switchTo', () => {
  it('pins the switched-to entry as active and resyncs', async () => {
    const store = createConnectionsStore();
    const owner = store.add('Owner', 'https://nymeria.example.com', 'nym_owner');

    await store.switchTo(owner.id);

    expect(store.activeConnectionId).toBe(owner.id);
    expect(configStore.apiUrl).toBe('https://nymeria.example.com');
    expect(threadsStore.reset).toHaveBeenCalledTimes(1);
    expect(threadsStore.syncFromBackend).toHaveBeenCalledTimes(1);
  });

  it('is a no-op for an unknown id', async () => {
    const store = createConnectionsStore();

    await store.switchTo('does-not-exist');

    expect(threadsStore.reset).not.toHaveBeenCalled();
    expect(threadsStore.syncFromBackend).not.toHaveBeenCalled();
  });
});
