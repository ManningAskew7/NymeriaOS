import { beforeEach, describe, expect, it, vi } from 'vitest';

// #445: a failed GET /mcp-servers left `loaded` false, so the panel effect
// that reads `loaded`/`loading` re-ran the load as fast as the backend
// refused it (connection-refused speed with the backend down), and the
// panel showed "No MCP servers yet" over the failure. The store now latches
// a failure (`loaded` + `error`) and a refusal (`forbidden`, silent, the
// #381 shape), a known non-admin never asks (the route is admin-only), and
// refresh() is the one way to ask again.
const reg = vi.hoisted(() => ({
  hooks: [] as Array<() => void>,
  identity: null as { role?: string } | null,
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: { listMCPServers: vi.fn() },
}));

vi.mock('./config.svelte', () => ({
  configStore: {
    get identity() {
      return reg.identity;
    },
  },
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
}));

vi.mock('$lib/services/api/humanizeError', () => ({
  humanizeErrorText: (e: unknown) => `humanized: ${(e as Error).message}`,
}));

import { createMCPServersStore } from './mcpServers.svelte';
import { api } from '$lib/services/api.svelte';
import type { MCPServer } from '$lib/types';

const listMock = api.listMCPServers as ReturnType<typeof vi.fn>;

function server(id: string): MCPServer {
  return { id, name: id, discoveredTools: [] } as unknown as MCPServer;
}

function statusError(status: number): Error {
  return Object.assign(new Error(`API error: ${status}`), { status });
}

function switchConnection(): void {
  for (const hook of reg.hooks) hook();
}

let store: ReturnType<typeof createMCPServersStore>;

beforeEach(() => {
  listMock.mockReset();
  reg.hooks.length = 0;
  reg.identity = null;
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  store = createMCPServersStore();
});

describe('mcpServersStore failure latch (#445)', () => {
  it('a failed load latches loaded + error: a second load() asks nothing', async () => {
    listMock.mockRejectedValue(new TypeError('Failed to fetch'));

    await store.load();
    await store.load();

    expect(listMock).toHaveBeenCalledTimes(1);
    expect(store.loaded).toBe(true);
    expect(store.loading).toBe(false);
    expect(store.error).toBe('humanized: Failed to fetch');
    expect(store.forbidden).toBe(false);
    expect(store.servers).toEqual([]);
  });

  it('refresh() asks exactly once more and a success clears the error', async () => {
    listMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await store.load();

    listMock.mockResolvedValueOnce({ servers: [server('github')], total: 1 });
    await store.refresh();

    expect(listMock).toHaveBeenCalledTimes(2);
    expect(store.error).toBeNull();
    expect(store.loaded).toBe(true);
    expect(store.servers.map((s) => s.id)).toEqual(['github']);
  });

  it('a failed refresh keeps the list it already had and reports the error', async () => {
    listMock.mockResolvedValueOnce({ servers: [server('github')], total: 1 });
    await store.load();

    listMock.mockRejectedValueOnce(statusError(500));
    await store.refresh();

    expect(store.error).toBe('humanized: API error: 500');
    expect(store.servers.map((s) => s.id)).toEqual(['github']);
    expect(store.loaded).toBe(true);
  });

  it('a 403 latches forbidden silently: no error copy and no repeat', async () => {
    listMock.mockRejectedValue(statusError(403));

    await store.load();
    await store.load();

    expect(listMock).toHaveBeenCalledTimes(1);
    expect(store.forbidden).toBe(true);
    expect(store.error).toBeNull();
    expect(store.loading).toBe(false);
  });

  it('a known non-admin never asks, on load or refresh', async () => {
    reg.identity = { role: 'user' };

    await store.load();
    await store.refresh();

    expect(listMock).not.toHaveBeenCalled();
    expect(store.forbidden).toBe(true);
    expect(store.error).toBeNull();
  });

  it('an admin identity asks as usual', async () => {
    reg.identity = { role: 'admin' };
    listMock.mockResolvedValue({ servers: [server('github')], total: 1 });

    await store.load();

    expect(listMock).toHaveBeenCalledTimes(1);
    expect(store.forbidden).toBe(false);
    expect(store.servers).toHaveLength(1);
  });

  it('a connection switch drops the latched failure and the refusal: the new backend is asked', async () => {
    listMock.mockRejectedValueOnce(statusError(403));
    await store.load();
    expect(store.forbidden).toBe(true);

    switchConnection();
    expect(store.forbidden).toBe(false);
    expect(store.error).toBeNull();
    expect(store.loaded).toBe(false);

    listMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await store.load();
    expect(store.error).toBe('humanized: Failed to fetch');

    switchConnection();
    expect(store.error).toBeNull();
    listMock.mockResolvedValueOnce({ servers: [server('b-server')], total: 1 });
    await store.load();

    expect(listMock).toHaveBeenCalledTimes(3);
    expect(store.servers.map((s) => s.id)).toEqual(['b-server']);
  });
});
