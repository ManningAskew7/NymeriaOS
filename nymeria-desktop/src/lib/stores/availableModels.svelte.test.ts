import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { flushSync } from 'svelte';
import type { AvailableModel } from '$lib/types';

// #453: the provider model pickers asked GET /models/available again on
// every settle whenever the list came back empty (the old helper read the
// state it wrote before its first await, and the api client folds every
// failure into []): about 1,100 requests in a 3.5 minute desktop run, 2,000
// on mobile, where Settings stays mounted and its effect was not gated on
// `open`, so it asked in the background for the app's lifetime. These run
// the real picker effect (`keepAvailableModelsLoaded`, which every panel
// calls) with each panel's inputs, over the real models store; the api
// client is the network boundary. Runes project only: the node
// environment's SSR transform never runs an effect.

const reg = vi.hoisted(() => ({ hooks: [] as Array<() => void> }));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getAvailableModels: vi.fn(),
    getOpenRouterModels: vi.fn(),
  },
}));

import { keepAvailableModelsLoaded, type AvailableModelsState } from './availableModels.svelte';
import { modelsStore } from './models.svelte';
import { api } from '$lib/services/api.svelte';

const getAvailable = api.getAvailableModels as Mock;

const WINDOW_MS = 60_000;
const T0 = 1_700_000_000_000;

function model(id: string): AvailableModel {
  return { id, name: id } as AvailableModel;
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

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

// What the panel holds: open (mobile panels stay mounted and pass null
// inputs while closed; desktop panels are mounted only while open), the
// provider and the base URL override, and a stand-in for the provider
// catalog that desktop's Model tab inputs also read (`supportsApiMode`).
const panel = $state({ open: true, provider: 'openrouter', baseUrl: '', catalog: 0 });

type Shape = 'desktop Model tab' | 'desktop Settings' | 'mobile Thread Settings' | 'mobile Settings';
const SHAPES: Shape[] = ['desktop Model tab', 'desktop Settings', 'mobile Thread Settings', 'mobile Settings'];

/** Mount a picker in one panel's shape; returns the state its dropdown renders. */
function mountPicker(shape: Shape): AvailableModelsState {
  const gatedOnOpen = shape.startsWith('mobile');
  let picker: AvailableModelsState | undefined;
  stop = $effect.root(() => {
    picker = keepAvailableModelsLoaded(() => {
      if (gatedOnOpen && !panel.open) return null;
      if (shape === 'desktop Model tab') void panel.catalog;
      return { provider: panel.provider, baseUrl: panel.baseUrl };
    });
  });
  return picker as AvailableModelsState;
}

let stop: () => void = () => undefined;
let now = T0;

beforeEach(() => {
  // Module-shared stores: every test starts from a fresh connection.
  switchConnection();
  getAvailable.mockReset();
  panel.open = true;
  panel.provider = 'openrouter';
  panel.baseUrl = '';
  panel.catalog = 0;
  now = T0;
  vi.spyOn(Date, 'now').mockImplementation(() => now);
});

afterEach(() => {
  stop();
  stop = () => undefined;
  vi.restoreAllMocks();
});

describe('an empty list is asked once per window, not once per settle', () => {
  it.each(SHAPES)('%s', async (shape) => {
    getAvailable.mockResolvedValue([]);
    const picker = mountPicker(shape);
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);
    expect(getAvailable).toHaveBeenCalledWith('openrouter', undefined);
    expect(picker.models).toEqual([]);
    expect(picker.loading).toBe(false);
  });

  it('a request that throws counts as empty', async () => {
    getAvailable.mockRejectedValue(new TypeError('Failed to fetch'));
    const picker = mountPicker('desktop Model tab');
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);
    expect(picker.loading).toBe(false);
  });

  it('reopening inside the window asks nothing; once it has passed, one more ask', async () => {
    getAvailable.mockResolvedValue([]);
    const picker = mountPicker('mobile Settings');
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);

    now = T0 + WINDOW_MS - 1;
    panel.open = false;
    await settle();
    panel.open = true;
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);

    now = T0 + WINDOW_MS;
    panel.open = false;
    await settle();
    getAvailable.mockResolvedValue([model('late-model')]);
    panel.open = true;
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);
    expect(picker.models.map((m) => m.id)).toEqual(['late-model']);
  });

  it('a non-empty list latches: reopening asks nothing, however late', async () => {
    getAvailable.mockResolvedValue([model('a-model'), model('b-model')]);
    const picker = mountPicker('mobile Thread Settings');
    await settle();
    expect(picker.models.map((m) => m.id)).toEqual(['a-model', 'b-model']);
    // Merged into the metadata store (context hints, effort clamps).
    expect(modelsStore.getById('a-model')).toBeDefined();

    now = T0 + 10 * WINDOW_MS;
    panel.open = false;
    await settle();
    panel.open = true;
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);
    expect(picker.models).toHaveLength(2);
  });

  it("an input re-run during the ask does not ask again (the Model tab's provider catalog landing)", async () => {
    const pending = deferred<AvailableModel[]>();
    getAvailable.mockReturnValueOnce(pending.promise);
    const picker = mountPicker('desktop Model tab');
    await settle();
    expect(picker.loading).toBe(true);

    panel.catalog += 1;
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);

    pending.resolve([model('a-model')]);
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);
    expect(picker.models.map((m) => m.id)).toEqual(['a-model']);
    expect(picker.loading).toBe(false);
  });

  it('no provider: nothing is asked and the list is empty', async () => {
    getAvailable.mockResolvedValue([model('a-model')]);
    panel.provider = '  ';
    const picker = mountPicker('desktop Settings');
    await settle();
    expect(getAvailable).not.toHaveBeenCalled();
    expect(picker.models).toEqual([]);
    expect(picker.loading).toBe(false);
  });
});

describe('a closed panel never asks', () => {
  it('mobile Settings stays mounted: nothing while closed, across a switch and time, then one ask on open', async () => {
    getAvailable.mockResolvedValue([]);
    panel.open = false;
    mountPicker('mobile Settings');
    await settle();
    switchConnection();
    now = T0 + 5 * WINDOW_MS;
    panel.provider = 'openai';
    await settle();
    expect(getAvailable).not.toHaveBeenCalled();

    panel.open = true;
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);
    expect(getAvailable).toHaveBeenCalledWith('openai', undefined);
  });
});

describe('a connection switch or a provider change asks at once', () => {
  it('a switch under an open picker asks the new backend once and drops the old list', async () => {
    getAvailable.mockResolvedValue([model('a-model')]);
    const picker = mountPicker('desktop Settings');
    await settle();
    expect(picker.models.map((m) => m.id)).toEqual(['a-model']);

    const fromB = deferred<AvailableModel[]>();
    getAvailable.mockReturnValueOnce(fromB.promise);
    switchConnection();
    flushSync();
    // The previous backend's list is gone before the new one answers.
    expect(picker.models).toEqual([]);
    expect(picker.loading).toBe(true);

    fromB.resolve([]);
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);
    expect(picker.models).toEqual([]);
    expect(picker.loading).toBe(false);
  });

  it('a switch clears an empty latch: the new backend is asked inside the old window', async () => {
    getAvailable.mockResolvedValue([]);
    const picker = mountPicker('mobile Settings');
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);

    getAvailable.mockResolvedValue([model('b-model')]);
    switchConnection();
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);
    expect(picker.models.map((m) => m.id)).toEqual(['b-model']);
  });

  it('a provider change asks the new provider at once, and a return to the old one asks again', async () => {
    getAvailable.mockResolvedValue([]);
    mountPicker('desktop Model tab');
    await settle();
    panel.provider = 'openai';
    await settle();
    panel.provider = 'openrouter';
    await settle();
    expect(getAvailable.mock.calls.map((call) => call[0])).toEqual(['openrouter', 'openai', 'openrouter']);
  });

  it('a base URL is part of the key and is sent', async () => {
    getAvailable.mockResolvedValue([]);
    panel.provider = 'openai';
    panel.baseUrl = 'http://localhost:11434/v1';
    mountPicker('mobile Thread Settings');
    await settle();
    panel.baseUrl = 'http://localhost:1234/v1';
    await settle();
    expect(getAvailable.mock.calls).toEqual([
      ['openai', 'http://localhost:11434/v1'],
      ['openai', 'http://localhost:1234/v1'],
    ]);
  });
});

describe('a late answer never lands on the wrong list', () => {
  it("the previous backend's answer, landing after a switch, lands nothing", async () => {
    const fromA = deferred<AvailableModel[]>();
    getAvailable.mockReturnValueOnce(fromA.promise);
    const picker = mountPicker('desktop Model tab');
    await settle();

    getAvailable.mockResolvedValueOnce([model('b-model')]);
    switchConnection();
    await settle();
    fromA.resolve([model('a-model')]);
    await settle();

    expect(getAvailable).toHaveBeenCalledTimes(2);
    expect(picker.models.map((m) => m.id)).toEqual(['b-model']);
    expect(picker.loading).toBe(false);
    expect(modelsStore.getById('a-model')).toBeUndefined();
  });

  it("a provider left mid-ask does not overwrite the new provider's list", async () => {
    const slow = deferred<AvailableModel[]>();
    getAvailable.mockReturnValueOnce(slow.promise);
    const picker = mountPicker('mobile Settings');
    await settle();

    getAvailable.mockResolvedValueOnce([model('openai-model')]);
    panel.provider = 'openai';
    await settle();
    slow.resolve([model('openrouter-model')]);
    await settle();

    expect(picker.models.map((m) => m.id)).toEqual(['openai-model']);
    expect(picker.loading).toBe(false);
  });
});
