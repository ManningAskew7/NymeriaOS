import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { flushSync } from 'svelte';

// #445: the panel load effects read a store's `loaded`/`loading` flags
// (directly, or inside the load call), so a load that settles without
// latching re-runs the effect, which asks again: one request per settle, as
// fast as the backend answers. These are real `$effect`s in the exact shapes
// the panels use (ThreadSettingsPanel and MCPServerPanel: `!loaded &&
// !loading`; SettingsPanel: the openrouter provider gate), over the real
// stores; the api client is the network boundary. Runes project only: the
// node environment's SSR transform never runs an effect.

const reg = vi.hoisted(() => ({
  hooks: [] as Array<() => void>,
  identity: null as { role?: string } | null,
}));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
  configStore: {
    get identity() {
      return reg.identity;
    },
  },
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getOpenRouterModels: vi.fn(),
    listMCPServers: vi.fn(),
  },
}));

vi.mock('$lib/services/api/humanizeError', () => ({
  humanizeErrorText: (e: unknown) => `humanized: ${(e as Error).message}`,
}));

import { modelsStore } from './models.svelte';
import { mcpServersStore } from './mcpServers.svelte';
import { api } from '$lib/services/api.svelte';

const getModels = api.getOpenRouterModels as Mock;
const listServers = api.listMCPServers as Mock;

/** Ten rounds of: run pending effects, then let the loads they started settle. */
async function settle(rounds = 10) {
  for (let round = 0; round < rounds; round += 1) {
    flushSync();
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  }
  flushSync();
}

function switchConnection(): void {
  for (const hook of reg.hooks) hook();
}

let stop: () => void = () => undefined;

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  reg.identity = null;
  // Module-shared stores: every test starts from a fresh connection.
  switchConnection();
  getModels.mockReset();
  listServers.mockReset();
});

afterEach(() => {
  stop();
  stop = () => undefined;
  vi.restoreAllMocks();
});

describe('models: an empty catalog is asked once, not once per settle', () => {
  it('the Thread Settings shape (`!loaded && !loading`)', async () => {
    getModels.mockResolvedValue([]);
    stop = $effect.root(() => {
      $effect(() => {
        if (!modelsStore.loaded && !modelsStore.loading) void modelsStore.loadModels();
      });
    });
    await settle();
    expect(getModels).toHaveBeenCalledTimes(1);
  });

  it('the Settings shape (gated on an openrouter provider)', async () => {
    getModels.mockResolvedValue([]);
    const provider = 'openrouter';
    stop = $effect.root(() => {
      $effect(() => {
        if (provider === 'openrouter') void modelsStore.loadModels();
      });
    });
    await settle();
    expect(getModels).toHaveBeenCalledTimes(1);
  });

  it('a failing request too', async () => {
    getModels.mockRejectedValue(new TypeError('Failed to fetch'));
    stop = $effect.root(() => {
      $effect(() => {
        if (!modelsStore.loaded && !modelsStore.loading) void modelsStore.loadModels();
      });
    });
    await settle();
    expect(getModels).toHaveBeenCalledTimes(1);
  });
});

describe('MCP servers: a failing load is asked once per mount, not once per settle', () => {
  function mountPanelEffect() {
    stop = $effect.root(() => {
      $effect(() => {
        if (!mcpServersStore.loaded && !mcpServersStore.loading) void mcpServersStore.load();
      });
    });
  }

  it('a refused connection: one request, and the error is kept for the panel', async () => {
    listServers.mockRejectedValue(new TypeError('Failed to fetch'));
    mountPanelEffect();
    await settle();
    expect(listServers).toHaveBeenCalledTimes(1);
    expect(mcpServersStore.error).toBe('humanized: Failed to fetch');
  });

  it('a 403: one request, no error copy', async () => {
    listServers.mockRejectedValue(Object.assign(new Error('API error: 403'), { status: 403 }));
    mountPanelEffect();
    await settle();
    expect(listServers).toHaveBeenCalledTimes(1);
    expect(mcpServersStore.error).toBeNull();
  });

  it('a connection switch under a live effect asks the new backend exactly once', async () => {
    listServers.mockRejectedValue(new TypeError('Failed to fetch'));
    mountPanelEffect();
    await settle();
    expect(listServers).toHaveBeenCalledTimes(1);

    switchConnection();
    await settle();
    expect(listServers).toHaveBeenCalledTimes(2);
  });
});
