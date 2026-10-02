import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// #445: an empty or failed GET /models used to leave the store retryable at
// once, so every panel effect that reads `loaded`/`loading` asked again as
// fast as the backend answered (and the backend answers an empty catalog
// from its own 60 s failure cache, so instantly). The store now waits out a
// retry window after an empty or failed load; a non-empty catalog still
// latches for the connection, and a connection switch clears the window.
const { getOpenRouterModelsMock, identityHooks } = vi.hoisted(() => ({
  getOpenRouterModelsMock: vi.fn(),
  identityHooks: [] as Array<() => void>,
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: { getOpenRouterModels: getOpenRouterModelsMock },
}));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (fn: () => void) => {
    identityHooks.push(fn);
    return () => undefined;
  },
}));

import { createModelsStore } from './models.svelte';
import type { ModelMetadata } from '$lib/types';

const WINDOW_MS = 60_000;
const T0 = 1_700_000_000_000;

function model(id: string, context_length = 200_000): ModelMetadata {
  return {
    id,
    name: id,
    context_length,
    max_completion_tokens: null,
    pricing_prompt: null,
    pricing_completion: null,
    supported_parameters: [],
    input_modalities: [],
    tokenizer: null,
    default_temperature: null,
    default_top_p: null,
    default_frequency_penalty: null,
  };
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function switchConnection(): void {
  for (const hook of identityHooks) hook();
}

describe('modelsStore load window (#445)', () => {
  let now = T0;
  let store: ReturnType<typeof createModelsStore>;

  beforeEach(() => {
    getOpenRouterModelsMock.mockReset();
    identityHooks.length = 0;
    now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    store = createModelsStore();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('asks once per window after an empty catalog, however often it is called', async () => {
    getOpenRouterModelsMock.mockResolvedValue([]);

    await store.loadModels();
    await store.loadModels();
    now = T0 + WINDOW_MS - 1;
    await store.loadModels();

    expect(getOpenRouterModelsMock).toHaveBeenCalledTimes(1);
    expect(store.loaded).toBe(false);
    expect(store.loading).toBe(false);
  });

  it('asks again once the window has passed, and a non-empty answer latches for the connection', async () => {
    getOpenRouterModelsMock.mockResolvedValueOnce([]);
    await store.loadModels();

    now = T0 + WINDOW_MS;
    getOpenRouterModelsMock.mockResolvedValueOnce([model('anthropic/claude-x', 1_000_000)]);
    await store.loadModels();

    expect(getOpenRouterModelsMock).toHaveBeenCalledTimes(2);
    expect(store.loaded).toBe(true);
    expect(store.getById('anthropic/claude-x')?.context_length).toBe(1_000_000);

    // Latched: far past any window, no further request.
    now = T0 + 10 * WINDOW_MS;
    await store.loadModels();
    expect(getOpenRouterModelsMock).toHaveBeenCalledTimes(2);
  });

  it('treats a thrown load like an empty one: one request per window', async () => {
    getOpenRouterModelsMock.mockRejectedValue(new Error('offline'));

    await store.loadModels();
    await store.loadModels();
    expect(getOpenRouterModelsMock).toHaveBeenCalledTimes(1);
    expect(store.loading).toBe(false);

    now = T0 + WINDOW_MS;
    await store.loadModels();
    expect(getOpenRouterModelsMock).toHaveBeenCalledTimes(2);
  });

  it('a connection switch clears the window: the new backend is asked at once', async () => {
    getOpenRouterModelsMock.mockResolvedValue([]);
    await store.loadModels();
    expect(getOpenRouterModelsMock).toHaveBeenCalledTimes(1);

    switchConnection();
    getOpenRouterModelsMock.mockResolvedValue([model('b-model')]);
    await store.loadModels();

    expect(getOpenRouterModelsMock).toHaveBeenCalledTimes(2);
    expect(store.loaded).toBe(true);
    expect(store.getById('b-model')).toBeDefined();
  });

  it('an empty answer from the previous backend does not hold back the new one', async () => {
    const pending = deferred<ModelMetadata[]>();
    getOpenRouterModelsMock.mockReturnValueOnce(pending.promise);
    const oldLoad = store.loadModels();

    switchConnection();
    pending.resolve([]);
    await oldLoad;

    getOpenRouterModelsMock.mockResolvedValueOnce([model('b-model')]);
    await store.loadModels();

    expect(getOpenRouterModelsMock).toHaveBeenCalledTimes(2);
    expect(store.loaded).toBe(true);
  });
});
