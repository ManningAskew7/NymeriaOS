import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

// #242, the per-app half of backendScopedStores.test.ts: the stores whose
// code differs between desktop and mobile, plus the desktop-only ones (the
// CLIProxy panel state, the three SSE-opened modals, profile pictures). After
// a connection switch nothing the previous backend served is readable and a
// response in flight across the switch lands nowhere. Hooks are captured
// from the real stores' registrations; the api is the network boundary.

const reg = vi.hoisted(() => {
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
  // profilePics reads localStorage at construction, before any test body.
  const storage = new MemoryStorage();
  (globalThis as { localStorage?: unknown }).localStorage = storage;
  return {
    hooks: [] as Array<() => void>,
    storage,
    apiUrl: 'http://localhost:8097',
    scope: null as { backend: string; accountId: string } | null,
  };
});

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
  currentIdentityScope: () => reg.scope,
  configStore: {
    get apiUrl() {
      return reg.apiUrl;
    },
  },
}));
vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getCustomTools: vi.fn(),
    getDefaultTools: vi.fn(),
    setDefaultTools: vi.fn(),
    createCustomTool: vi.fn(),
    listMCPServers: vi.fn(),
    getNotifications: vi.fn(),
    getNotificationChannelTypes: vi.fn(),
    listNotificationDestinations: vi.fn(),
    listNotificationProfiles: vi.fn(),
    getNotificationPreferences: vi.fn(),
    getTodos: vi.fn(),
    getActivity: vi.fn(),
    getCLIProxyStatus: vi.fn(),
    listCLIProxyAuthFiles: vi.fn(),
    startCLIProxyOAuth: vi.fn(),
    getCLIProxyOAuthStatus: vi.fn(),
    submitUiPromptResult: vi.fn().mockResolvedValue(undefined),
    endBrowserLoginSession: vi.fn().mockResolvedValue(undefined),
  },
}));
vi.mock('$lib/services/api/humanizeError', () => ({
  humanizeErrorText: (e: unknown) => `humanized: ${(e as Error).message}`,
}));
vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn().mockRejectedValue(new Error('no tauri runtime')),
}));
vi.mock('@tauri-apps/plugin-opener', () => ({ openUrl: vi.fn().mockResolvedValue(undefined) }));

import { toolsStore } from './tools.svelte';
import { defaultToolsStore } from './defaultTools.svelte';
import { mcpServersStore } from './mcpServers.svelte';
import { notificationStore } from './notifications.svelte';
import { todosStore } from './todos.svelte';
import { activityStore } from './activity.svelte';
import { cliproxyStore } from './cliproxy.svelte';
import { uiPromptStore } from './uiPrompt.svelte';
import { authPromptStore } from './authPrompt.svelte';
import { browserLoginStore } from './browserLogin.svelte';
import { profilePics } from './profilePics.svelte';
import { api } from '$lib/services/api.svelte';
import type { AuthPromptEvent, CLIProxyProviderInfo, UiPromptEvent } from '$lib/types';
import type { BrowserLoginSessionStatus } from '$lib/services/api/browser-login';

const A = 'http://localhost:8097';
const B = 'http://localhost:8098';

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
  reg.apiUrl = A;
  reg.scope = null;
  switchBackend();
});

afterEach(() => {
  vi.useRealTimers();
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
  it('a switch clears the bell list, unread count and the config lists', async () => {
    (api.getNotifications as Mock).mockResolvedValueOnce({
      notifications: [{ id: 'n1', title: 'Done', read: false }],
      unreadCount: 1,
    });
    await notificationStore.fetch();
    (api.getNotificationChannelTypes as Mock).mockResolvedValueOnce([{ type: 'discord' }]);
    (api.listNotificationDestinations as Mock).mockResolvedValueOnce([{ id: 'd1', name: 'ops' }]);
    (api.listNotificationProfiles as Mock).mockResolvedValueOnce([{ id: 'p1', destinationNames: ['ops'] }]);
    (api.getNotificationPreferences as Mock).mockResolvedValueOnce({ quiet: true });
    await notificationStore.loadConfig();
    expect(notificationStore.unreadCount).toBe(1);
    expect(notificationStore.destinations).toHaveLength(1);

    switchBackend();
    expect(notificationStore.notifications).toEqual([]);
    expect(notificationStore.unreadCount).toBe(0);
    expect(notificationStore.channelTypes).toEqual([]);
    expect(notificationStore.destinations).toEqual([]);
    expect(notificationStore.profiles).toEqual([]);
    expect(notificationStore.preferences).toBeNull();
  });

  it('a poll in flight across the switch lands nothing', async () => {
    const stale = deferred<{ notifications: unknown[]; unreadCount: number }>();
    (api.getNotifications as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = notificationStore.fetch();
    switchBackend();
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

describe('CLIProxy panel state', () => {
  const CLAUDE: CLIProxyProviderInfo = {
    id: 'claude',
    label: 'Claude',
    description: '',
    flow: 'browser',
    nymeria_provider: 'anthropic',
    url_shape: 'root',
    api_mode: '',
    key_env_var: 'ANTHROPIC_API_KEY',
    default_model: 'claude-opus-5',
    tos_warning: '',
    auth_file_provider: 'claude',
    auth_file_providers: ['claude'],
    supported: true,
    logged_in: true,
  };

  it('a switch drops the proxy status and auth files and stops an OAuth poll against the old login', async () => {
    vi.useFakeTimers();
    (api.getCLIProxyStatus as Mock).mockResolvedValueOnce({ reachable: true, configured: true, providers: [CLAUDE] });
    (api.listCLIProxyAuthFiles as Mock).mockResolvedValueOnce([{ name: 'claude-a.json', provider: 'claude' }]);
    await cliproxyStore.refresh();
    (api.startCLIProxyOAuth as Mock).mockResolvedValueOnce({ flow: 'browser', url: 'https://login', state: 'st-a' });
    await cliproxyStore.startOAuth(CLAUDE);
    expect(cliproxyStore.oauth?.state).toBe('st-a');
    expect(cliproxyStore.authFiles).toHaveLength(1);

    switchBackend();
    expect(cliproxyStore.status).toBeNull();
    expect(cliproxyStore.authFiles).toEqual([]);
    expect(cliproxyStore.oauth).toBeNull();
    // The poll timer itself is gone, not just idling until its next tick.
    expect(vi.getTimerCount()).toBe(0);

    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.getCLIProxyOAuthStatus).not.toHaveBeenCalled();
  });

  it('a login poll in flight across the switch marks nothing logged in on the new backend', async () => {
    vi.useFakeTimers();
    (api.startCLIProxyOAuth as Mock).mockResolvedValueOnce({ flow: 'browser', url: 'https://login', state: 'st-a' });
    await cliproxyStore.startOAuth(CLAUDE);
    const stale = deferred<{ status: string; detail?: string }>();
    (api.getCLIProxyOAuthStatus as Mock).mockReturnValueOnce(stale.promise);
    await vi.advanceTimersToNextTimerAsync();
    expect(api.getCLIProxyOAuthStatus).toHaveBeenCalledTimes(1);

    switchBackend();
    stale.resolve({ status: 'ok', detail: 'a@example.com' });
    await vi.advanceTimersByTimeAsync(0);

    expect(cliproxyStore.oauth).toBeNull();
    expect(cliproxyStore.message).toBeNull();
    expect(api.getCLIProxyStatus).not.toHaveBeenCalled();
  });

  it('a status refresh in flight across the switch lands nothing', async () => {
    const stale = deferred<unknown>();
    (api.getCLIProxyStatus as Mock).mockReturnValueOnce(stale.promise);
    const inFlight = cliproxyStore.refresh();
    switchBackend();
    stale.resolve({ reachable: true, configured: true, providers: [CLAUDE] });
    await inFlight;
    expect(cliproxyStore.status).toBeNull();
    expect(api.listCLIProxyAuthFiles).not.toHaveBeenCalled();
  });
});

describe('SSE-opened modals close on a switch (a submit would post the old prompt id to the new backend)', () => {
  it('ui prompt, credential prompt and browser login all clear, with nothing posted to the new backend', () => {
    uiPromptStore.open({ prompt_id: 'ui-1', thread_id: 't', title: '', html: '<p/>', timeout_seconds: 60, expires_at: null } as UiPromptEvent);
    // Partial fixtures: the stores only key on the prompt/session id.
    authPromptStore.open({ prompt_id: 'auth-1' } as unknown as AuthPromptEvent);
    browserLoginStore.open({ session_id: 'bl-1', state: 'active', seconds_remaining: 600 } as BrowserLoginSessionStatus, 'agent');
    expect(uiPromptStore.active?.prompt_id).toBe('ui-1');
    expect(authPromptStore.active?.prompt_id).toBe('auth-1');
    expect(browserLoginStore.active?.session.session_id).toBe('bl-1');

    switchBackend();
    expect(uiPromptStore.active).toBeNull();
    expect(authPromptStore.active).toBeNull();
    expect(browserLoginStore.active).toBeNull();
    expect(api.submitUiPromptResult).not.toHaveBeenCalled();
    expect(api.endBrowserLoginSession).not.toHaveBeenCalled();
  });
});

describe('profile pictures are kept per backend + account', () => {
  it('backend B`s `default` never shows backend A`s upload', () => {
    reg.apiUrl = A;
    profilePics.set('default', 'data:image/png;base64,AAAA');
    expect(profilePics.get('default')).toBe('data:image/png;base64,AAAA');

    reg.apiUrl = B;
    expect(profilePics.get('default')).toBeNull();
    // A saved connection's avatar still finds its own backend's picture.
    expect(profilePics.get('default', A)).toBe('data:image/png;base64,AAAA');

    profilePics.clear('default', A);
    expect(profilePics.get('default', A)).toBeNull();
  });

  it('a picture saved under the bare account id moves to the first backend that resolves it, once', () => {
    reg.storage.setItem('nymeria_profile_pic_alice', 'data:image/png;base64,OLD');
    reg.scope = { backend: A, accountId: 'alice' };
    switchBackend();
    expect(profilePics.get('alice', A)).toBe('data:image/png;base64,OLD');
    expect(reg.storage.getItem('nymeria_profile_pic_alice')).toBeNull();

    reg.scope = { backend: B, accountId: 'alice' };
    switchBackend();
    expect(profilePics.get('alice', B)).toBeNull();
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
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['b_1', 'mcp__gh__issues']));
    expect(await defaultToolsStore.toggleDefaultTool('mcp__gh__issues')).toBe(true);
    expect(api.setDefaultTools).toHaveBeenLastCalledWith(['b_1', 'mcp__gh__issues'], undefined);
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['mcp__gh__issues']));
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
    await defaultToolsStore.reload();
    // The desktop save re-reads the list it saved.
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['b_1']));
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
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['a_1', 'mcp__gh__issues']));
    expect(await defaultToolsStore.toggleDefaultTool('mcp__gh__issues')).toBe(true);
    expect(api.setDefaultTools).toHaveBeenLastCalledWith(['a_1', 'mcp__gh__issues'], undefined);
  });

  it('a second toggle waits out the first; a save in flight across the switch lands nothing', async () => {
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['a_1']));
    await defaultToolsStore.load();
    const stale = deferred<void>();
    (api.setDefaultTools as Mock).mockReturnValueOnce(stale.promise);
    (api.getDefaultTools as Mock).mockResolvedValueOnce(list(['a_1', 'mcp__x__one']));
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
