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
    getDefaultTools: vi.fn(),
    setDefaultTools: vi.fn(),
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
import { defaultToolsStore } from './defaultTools.svelte';
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

describe('default-tool toggles save the whole list, so only a list loaded from this backend (#447)', () => {
  const list = (names: string[]) => ({ available_tools: [], default_tools: names, callable_thread_count: 0 });

  it('after a switch reset the store a toggle sends nothing; once B`s list loads it edits B`s list', async () => {
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['a_1', 'a_2']));
    await defaultToolsStore.load();
    switchBackend();

    expect(defaultToolsStore.listReady).toBe(false);
    expect(await defaultToolsStore.toggleDefaultTool('mcp__gh__issues')).toBe(false);
    expect(api.setDefaultTools).not.toHaveBeenCalled();

    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['b_1']));
    await defaultToolsStore.load();
    expect(await defaultToolsStore.toggleDefaultTool('mcp__gh__issues')).toBe(true);
    expect(api.setDefaultTools).toHaveBeenLastCalledWith(['b_1', 'mcp__gh__issues'], undefined);
    expect(await defaultToolsStore.toggleDefaultTool('b_1')).toBe(true);
    expect(api.setDefaultTools).toHaveBeenLastCalledWith(['mcp__gh__issues'], undefined);
  });

  it('a whole-list save() refuses the same way: a selection built before a switch, or on a failed load, never reaches B (delta review LOW-2)', async () => {
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['a_1', 'a_2']));
    await defaultToolsStore.load();
    switchBackend();

    // A settings panel's selection seeded from A's list, saved after the switch.
    expect(await defaultToolsStore.save(['a_1'])).toBe(false);
    // B's first load fails: the list is empty, `loaded` latches, and a
    // selection seeded from it would PUT a subset over B's whole set.
    (api.getDefaultTools as Mock).mockRejectedValueOnce(new Error('Not signed in'));
    await defaultToolsStore.load();
    expect(defaultToolsStore.loaded).toBe(true);
    expect(await defaultToolsStore.save(['mcp__gh__issues'])).toBe(false);
    expect(api.setDefaultTools).not.toHaveBeenCalled();
    expect(defaultToolsStore.saving).toBe(false);
    expect(defaultToolsStore.defaultToolNames).toEqual([]);

    // Once B's list has landed, a selection seeded from it saves.
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['b_1', 'b_2']));
    defaultToolsStore.resetLoaded();
    await defaultToolsStore.load();
    expect(await defaultToolsStore.save(['b_1'])).toBe(true);
    expect(api.setDefaultTools).toHaveBeenCalledTimes(1);
    expect(api.setDefaultTools).toHaveBeenLastCalledWith(['b_1'], undefined);
    expect(defaultToolsStore.defaultToolNames).toEqual(['b_1']);
  });

  it('a failed load latches `loaded` (no effect loop) but never unlocks the toggle', async () => {
    (api.getDefaultTools as Mock).mockRejectedValueOnce(new Error('503'));
    await defaultToolsStore.load();
    expect(defaultToolsStore.loaded).toBe(true);
    expect(defaultToolsStore.listReady).toBe(false);
    expect(await defaultToolsStore.toggleDefaultTool('mcp__gh__issues')).toBe(false);
    expect(api.setDefaultTools).not.toHaveBeenCalled();
  });

  it('a reload that fails after a good load locks the toggle again (the held list may be out of date)', async () => {
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['a_1']));
    await defaultToolsStore.load();
    expect(defaultToolsStore.listReady).toBe(true);

    defaultToolsStore.resetLoaded();
    (api.getDefaultTools as Mock).mockRejectedValueOnce(new Error('503'));
    await defaultToolsStore.load();
    expect(defaultToolsStore.listReady).toBe(false);
    expect(await defaultToolsStore.toggleDefaultTool('mcp__gh__issues')).toBe(false);
    expect(api.setDefaultTools).not.toHaveBeenCalled();
  });

  it('a failed save leaves the loaded list editable', async () => {
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['a_1']));
    await defaultToolsStore.load();
    (api.setDefaultTools as Mock).mockRejectedValueOnce(new Error('500'));
    expect(await defaultToolsStore.toggleDefaultTool('mcp__gh__issues')).toBe(false);
    expect(defaultToolsStore.error).not.toBeNull();

    (api.setDefaultTools as Mock).mockResolvedValueOnce(undefined);
    expect(await defaultToolsStore.toggleDefaultTool('mcp__gh__issues')).toBe(true);
    expect(api.setDefaultTools).toHaveBeenLastCalledWith(['a_1', 'mcp__gh__issues'], undefined);
  });

  it('a second toggle waits out the first; a save in flight across the switch lands nothing', async () => {
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['a_1']));
    await defaultToolsStore.load();
    const stale = deferred<void>();
    (api.setDefaultTools as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = defaultToolsStore.toggleDefaultTool('mcp__x__one');
    expect(await defaultToolsStore.toggleDefaultTool('mcp__x__two')).toBe(false);

    switchBackend();
    stale.resolve();
    expect(await inFlight).toBe(false);
    expect(defaultToolsStore.defaultToolNames).toEqual([]);
    expect(defaultToolsStore.listReady).toBe(false);
    expect(defaultToolsStore.saving).toBe(false);
    expect(api.setDefaultTools).toHaveBeenCalledTimes(1);
  });
});
