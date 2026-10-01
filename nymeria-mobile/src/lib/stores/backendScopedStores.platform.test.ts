import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

// #242, the per-app half of backendScopedStores.test.ts: the stores whose
// code differs between desktop and mobile. After a connection switch nothing
// the previous backend served is readable and a response in flight across the
// switch lands nowhere. Hooks are captured from the real stores'
// registrations; the api is the network boundary. Mobile has no notification
// config lists and none of desktop's CLIProxy/prompt/profile-picture stores.

const reg = vi.hoisted(() => ({ hooks: [] as Array<() => void> }));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
}));
vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getCustomTools: vi.fn(),
    listMCPServers: vi.fn(),
    getNotifications: vi.fn(),
    getTodos: vi.fn(),
    getActivity: vi.fn(),
  },
}));
vi.mock('$lib/services/api/humanizeError', () => ({
  humanizeErrorText: (e: unknown) => `humanized: ${(e as Error).message}`,
}));

import { toolsStore } from './tools.svelte';
import { mcpServersStore } from './mcpServers.svelte';
import { notificationStore } from './notifications.svelte';
import { todosStore } from './todos.svelte';
import { activityStore } from './activity.svelte';
import { api } from '$lib/services/api.svelte';

function switchBackend(): void {
  for (const hook of reg.hooks) hook();
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  switchBackend();
});

describe('custom tools', () => {
  it('a switch drops the list and selection and re-arms the load gate', async () => {
    (api.getCustomTools as Mock).mockResolvedValueOnce({ tools: [{ id: 't1', name: 'weather', enabled: true }] });
    await toolsStore.loadTools();
    toolsStore.selectTool('t1');
    expect(toolsStore.tools).toHaveLength(1);

    switchBackend();
    expect(toolsStore.tools).toEqual([]);
    expect(toolsStore.selectedToolId).toBeNull();
    expect(toolsStore.loaded).toBe(false);
  });

  it('a load in flight across the switch lands nothing and does not mark the new backend loaded', async () => {
    const stale = deferred<{ tools: unknown[] }>();
    (api.getCustomTools as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = toolsStore.loadTools();
    switchBackend();
    stale.resolve({ tools: [{ id: 't1', name: 'weather', enabled: true }] });
    await inFlight;
    expect(toolsStore.tools).toEqual([]);
    expect(toolsStore.loaded).toBe(false);
  });
});

describe('MCP servers (human-chosen ids collide across backends)', () => {
  it('a switch drops the list; a load in flight lands nothing', async () => {
    (api.listMCPServers as Mock).mockResolvedValueOnce({ servers: [{ id: 'github', name: 'GitHub' }] });
    await mcpServersStore.load();
    expect(mcpServersStore.getServer('github')).toBeDefined();

    const stale = deferred<{ servers: unknown[] }>();
    (api.listMCPServers as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = mcpServersStore.refresh();
    switchBackend();
    expect(mcpServersStore.getServer('github')).toBeUndefined();
    expect(mcpServersStore.loaded).toBe(false);

    stale.resolve({ servers: [{ id: 'github', name: 'GitHub' }] });
    await inFlight;
    expect(mcpServersStore.getServer('github')).toBeUndefined();
    expect(mcpServersStore.loaded).toBe(false);
  });
});

describe('notifications', () => {
  it('a switch clears the list and unread count; a poll in flight lands nothing', async () => {
    (api.getNotifications as Mock).mockResolvedValueOnce({
      notifications: [{ id: 'n1', title: 'Done', read: false }],
      unreadCount: 1,
    });
    await notificationStore.fetch();
    expect(notificationStore.unreadCount).toBe(1);

    const stale = deferred<{ notifications: unknown[]; unreadCount: number }>();
    (api.getNotifications as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = notificationStore.fetch();
    switchBackend();
    expect(notificationStore.notifications).toEqual([]);
    expect(notificationStore.unreadCount).toBe(0);

    stale.resolve({ notifications: [{ id: 'n1', title: 'Done', read: false }], unreadCount: 1 });
    await inFlight;
    expect(notificationStore.notifications).toEqual([]);
    expect(notificationStore.unreadCount).toBe(0);
  });
});

describe('scheduled tasks and the activity feed', () => {
  it('a switch clears both; fetches in flight land nothing', async () => {
    (api.getTodos as Mock).mockResolvedValueOnce({ items: [{ id: 'todo1', status: 'pending' }] });
    await todosStore.fetch();
    (api.getActivity as Mock).mockResolvedValueOnce({ entries: [{ id: 'a1' }] });
    await activityStore.fetch();
    expect(todosStore.todos).toHaveLength(1);
    expect(activityStore.entries).toHaveLength(1);

    const staleTodos = deferred<{ items: unknown[] }>();
    const staleActivity = deferred<{ entries: unknown[] }>();
    (api.getTodos as Mock).mockReturnValueOnce(staleTodos.promise);
    (api.getActivity as Mock).mockReturnValueOnce(staleActivity.promise);
    const todosInFlight = todosStore.fetch();
    const activityInFlight = activityStore.fetch();

    switchBackend();
    expect(todosStore.todos).toEqual([]);
    expect(activityStore.entries).toEqual([]);

    staleTodos.resolve({ items: [{ id: 'todo1', status: 'pending' }] });
    staleActivity.resolve({ entries: [{ id: 'a1' }] });
    await Promise.all([todosInFlight, activityInFlight]);
    expect(todosStore.todos).toEqual([]);
    expect(activityStore.entries).toEqual([]);
    expect(todosStore.loading).toBe(false);
  });
});
