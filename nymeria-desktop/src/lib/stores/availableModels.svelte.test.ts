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
    createCredential: vi.fn(),
    updateCredential: vi.fn(),
    deleteCredential: vi.fn(),
  },
}));

import {
  keepAvailableModelsLoaded,
  refreshAvailableModels,
  type AvailableModelsState,
} from './availableModels.svelte';
import { credentialsStore } from './credentials.svelte';
import { modelsStore } from './models.svelte';
import { api } from '$lib/services/api.svelte';
import type { Credential } from '$lib/types';

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

/**
 * A connection switch: the store's reload hook (registered at import), then
 * the Settings panels' GET /settings snapshot drop (registered on mount),
 * which leaves their form unseeded until the reload lands.
 */
function switchConnection(): void {
  for (const hook of reg.hooks) hook();
  panel.seeded = false;
}

// What the panel holds: open (mobile panels stay mounted and pass null
// inputs while closed; desktop panels are mounted only while open), whether
// GET /settings has seeded a Settings panel's form (both pass null until
// then), the provider and the base URL override, whether the provider takes
// a base URL (the thread pickers send it only then, `supportsApiMode`), and
// a stand-in for the provider catalog that desktop's Model tab inputs also
// read.
const panel = $state({
  open: true,
  seeded: true,
  provider: 'openrouter',
  baseUrl: '',
  takesBaseUrl: true,
  catalog: 0,
});

type Shape = 'desktop Model tab' | 'desktop Settings' | 'mobile Thread Settings' | 'mobile Settings';
const SHAPES: Shape[] = ['desktop Model tab', 'desktop Settings', 'mobile Thread Settings', 'mobile Settings'];
const SETTINGS_SHAPES: Shape[] = ['desktop Settings', 'mobile Settings'];
const THREAD_SHAPES: Shape[] = ['desktop Model tab', 'mobile Thread Settings'];

/** Mount a picker in one panel's shape; returns the state its dropdown renders. */
function mountPicker(shape: Shape): AvailableModelsState {
  const gatedOnOpen = shape.startsWith('mobile');
  const settings = SETTINGS_SHAPES.includes(shape);
  let picker: AvailableModelsState | undefined;
  stop = $effect.root(() => {
    picker = keepAvailableModelsLoaded(() => {
      if (gatedOnOpen && !panel.open) return null;
      if (settings && !panel.seeded) return null;
      if (shape === 'desktop Model tab') void panel.catalog;
      const baseUrl = settings || panel.takesBaseUrl ? panel.baseUrl : '';
      return { provider: panel.provider, baseUrl };
    });
  });
  return picker as AvailableModelsState;
}

function credential(id: string): Credential {
  return { id, name: id, provider: 'openai', status: 'active' } as Credential;
}

let stop: () => void = () => undefined;
let now = T0;

beforeEach(() => {
  // Module-shared stores: every test starts from a fresh connection.
  switchConnection();
  getAvailable.mockReset();
  (api.createCredential as Mock).mockReset();
  (api.updateCredential as Mock).mockReset();
  (api.deleteCredential as Mock).mockReset();
  panel.open = true;
  panel.seeded = true;
  panel.provider = 'openrouter';
  panel.baseUrl = '';
  panel.takesBaseUrl = true;
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
    panel.seeded = true; // the open panel's GET /settings lands
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);
    expect(getAvailable).toHaveBeenCalledWith('openai', undefined);
  });
});

describe('a Settings panel asks nothing until GET /settings has seeded its form', () => {
  it.each(SETTINGS_SHAPES)('%s: no ask for the hard-coded default provider, then one for the seeded one', async (shape) => {
    getAvailable.mockResolvedValue([]);
    // The form's defaults before the snapshot lands.
    panel.seeded = false;
    panel.provider = 'anthropic';
    panel.baseUrl = '';
    mountPicker(shape);
    await settle();
    expect(getAvailable).not.toHaveBeenCalled();

    // The seed writes the form and the snapshot in one go.
    panel.provider = 'openrouter';
    panel.baseUrl = 'http://localhost:8317';
    panel.seeded = true;
    await settle();
    expect(getAvailable.mock.calls).toEqual([['openrouter', 'http://localhost:8317']]);
  });

  it.each(SETTINGS_SHAPES)('%s: a switch drops the old list at once and asks once the form is reseeded', async (shape) => {
    getAvailable.mockResolvedValue([model('a-model')]);
    const picker = mountPicker(shape);
    await settle();
    expect(picker.models.map((m) => m.id)).toEqual(['a-model']);

    switchConnection();
    flushSync();
    // The previous backend's list is gone while the form is unseeded.
    expect(picker.models).toEqual([]);
    expect(picker.loading).toBe(false);
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);

    getAvailable.mockResolvedValue([model('b-model')]);
    panel.seeded = true;
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);
    expect(picker.models.map((m) => m.id)).toEqual(['b-model']);
  });

  it('a switch mid-ask stops the spinner, and the late answer lands nothing', async () => {
    const fromA = deferred<AvailableModel[]>();
    getAvailable.mockReturnValueOnce(fromA.promise);
    const picker = mountPicker('desktop Settings');
    flushSync();
    expect(picker.loading).toBe(true);

    switchConnection();
    flushSync();
    expect(picker.loading).toBe(false);

    fromA.resolve([model('a-model')]);
    await settle();
    expect(picker.models).toEqual([]);
    expect(picker.loading).toBe(false);
    expect(modelsStore.getById('a-model')).toBeUndefined();
  });
});

describe('a connection switch or a provider change asks at once', () => {
  it('a switch under an open picker asks the new backend once and drops the old list', async () => {
    getAvailable.mockResolvedValue([model('a-model')]);
    const picker = mountPicker('desktop Model tab');
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
    panel.seeded = true; // the reload lands
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);
    expect(picker.models.map((m) => m.id)).toEqual(['b-model']);
  });

  it("a provider change drops the previous provider's list while the new one loads", async () => {
    getAvailable.mockResolvedValueOnce([model('openrouter-model')]);
    const picker = mountPicker('desktop Settings');
    await settle();
    expect(picker.models.map((m) => m.id)).toEqual(['openrouter-model']);

    const slow = deferred<AvailableModel[]>();
    getAvailable.mockReturnValueOnce(slow.promise);
    panel.provider = 'anthropic';
    flushSync();
    // Not selectable under the new provider while its answer loads.
    expect(picker.models).toEqual([]);
    expect(picker.loading).toBe(true);

    slow.resolve([]);
    await settle();
    expect(picker.models).toEqual([]);
    expect(picker.loading).toBe(false);
  });

  it.each(THREAD_SHAPES)('%s: the base URL is sent only where the provider takes one', async (shape) => {
    getAvailable.mockResolvedValue([]);
    panel.provider = 'anthropic';
    panel.baseUrl = 'http://localhost:1234/v1';
    panel.takesBaseUrl = false;
    mountPicker(shape);
    await settle();
    expect(getAvailable.mock.calls).toEqual([['anthropic', undefined]]);
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

describe('a save that can change the answer re-asks at once', () => {
  // GET /models/available caches nothing, so after a provider settings or
  // credential save the next ask can list what the last one could not.
  it('a refresh re-asks a latched empty list inside the window, once', async () => {
    getAvailable.mockResolvedValue([]);
    const picker = mountPicker('desktop Settings');
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);

    getAvailable.mockResolvedValue([model('a-model')]);
    refreshAvailableModels();
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);
    expect(picker.models.map((m) => m.id)).toEqual(['a-model']);

    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);
  });

  it('a refresh re-asks a non-empty list too, keeping it on screen until the answer lands', async () => {
    getAvailable.mockResolvedValueOnce([model('old-model')]);
    const picker = mountPicker('mobile Thread Settings');
    await settle();

    const pending = deferred<AvailableModel[]>();
    getAvailable.mockReturnValueOnce(pending.promise);
    refreshAvailableModels();
    flushSync();
    expect(getAvailable).toHaveBeenCalledTimes(2);
    expect(picker.models.map((m) => m.id)).toEqual(['old-model']);
    expect(picker.loading).toBe(true);

    pending.resolve([model('new-model')]);
    await settle();
    expect(picker.models.map((m) => m.id)).toEqual(['new-model']);
    expect(picker.loading).toBe(false);
  });

  it('a closed panel asks nothing on a refresh, then once when reopened inside the window', async () => {
    getAvailable.mockResolvedValue([]);
    mountPicker('mobile Settings');
    await settle();
    panel.open = false;
    await settle();

    refreshAvailableModels();
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);

    panel.open = true;
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);
  });

  it('a credential saved or disabled re-asks an open picker; a failed save does not', async () => {
    getAvailable.mockResolvedValue([]);
    mountPicker('desktop Settings');
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(1);

    (api.createCredential as Mock).mockResolvedValueOnce(credential('c1'));
    expect(await credentialsStore.create({ owner_type: 'user', name: 'c1', provider: 'openai', kind: 'api_key' })).not.toBeNull();
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);

    (api.updateCredential as Mock).mockRejectedValueOnce(new Error('refused'));
    expect(await credentialsStore.update('c1', { name: 'c1' })).toBeNull();
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(2);

    (api.updateCredential as Mock).mockResolvedValueOnce(credential('c1'));
    await credentialsStore.update('c1', { name: 'c1' });
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(3);

    (api.deleteCredential as Mock).mockResolvedValueOnce(undefined);
    expect(await credentialsStore.disable('c1')).toBe(true);
    await settle();
    expect(getAvailable).toHaveBeenCalledTimes(4);
  });
});
