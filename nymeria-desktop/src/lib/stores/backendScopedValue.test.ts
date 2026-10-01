import { beforeEach, describe, expect, it, vi } from 'vitest';

// #242 review S-MED-2: the settings panels seed a whole form from their
// GET /settings snapshot and Save sends the tab back whole. The desktop
// panel's edit-the-active-connection path re-applied the connection without
// dropping that snapshot, so after pointing the entry at backend B a Save of
// one LLM field PATCHed backend A's provider, model and base URL onto B.
// The snapshot now lives in `createBackendScopedValue`, which every identity
// reload hook (every switch, from any path) empties. The reload hooks are
// captured from the real registration; the fetches are the network boundary.

const reg = vi.hoisted(() => ({ hooks: new Set<() => void>() }));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.add(hook);
    return () => reg.hooks.delete(hook);
  },
}));

import { createBackendScopedValue } from './backendScopedValue.svelte';

type Settings = { llm_model: string; llm_base_url: string };

function settingsOf(tag: string): Settings {
  return { llm_model: `model-${tag}`, llm_base_url: `https://${tag}.example/v1` };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** What a connection switch does to the panel: every reload hook fires. */
function switchBackend() {
  for (const hook of [...reg.hooks]) hook();
}

beforeEach(() => {
  reg.hooks.clear();
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

describe('a settings form source that belongs to one backend', () => {
  it('a switch drops the snapshot, so the next load seeds the form from the new backend', async () => {
    const slot = createBackendScopedValue<Settings>('server settings');
    slot.attach();
    const seeded: Settings[] = [];

    expect(await slot.load(async () => settingsOf('a'), (s) => seeded.push(s))).toBe(true);
    expect(slot.value).toEqual(settingsOf('a'));

    switchBackend();
    expect(slot.value).toBeNull();
    expect(slot.loading).toBe(false);
    expect(slot.failed).toBe(false);

    expect(await slot.load(async () => settingsOf('b'), (s) => seeded.push(s))).toBe(true);
    expect(slot.value).toEqual(settingsOf('b'));
    expect(seeded).toEqual([settingsOf('a'), settingsOf('b')]);
  });

  it('a load in flight across the switch neither lands nor seeds, and does not block the new load', async () => {
    const slot = createBackendScopedValue<Settings>('server settings');
    slot.attach();
    const seeded: Settings[] = [];
    const fromA = deferred<Settings>();

    const stale = slot.load(() => fromA.promise, (s) => seeded.push(s));
    expect(slot.loading).toBe(true);
    switchBackend();
    expect(slot.loading).toBe(false);

    expect(await slot.load(async () => settingsOf('b'), (s) => seeded.push(s))).toBe(true);
    fromA.resolve(settingsOf('a'));
    expect(await stale).toBe(false);

    expect(slot.value).toEqual(settingsOf('b'));
    expect(slot.loading).toBe(false);
    expect(seeded).toEqual([settingsOf('b')]);
  });

  it('a failed load latches `failed` (no retry loop) until the next load or switch', async () => {
    const slot = createBackendScopedValue<Settings>('server settings');
    slot.attach();

    expect(await slot.load(async () => Promise.reject(new Error('503')))).toBe(false);
    expect(slot.failed).toBe(true);
    expect(slot.loading).toBe(false);
    expect(slot.value).toBeNull();

    switchBackend();
    expect(slot.failed).toBe(false);
  });

  it('a failure in flight across the switch does not mark the new backend failed', async () => {
    const slot = createBackendScopedValue<Settings>('server settings');
    slot.attach();
    const fromA = deferred<Settings>();

    const stale = slot.load(() => fromA.promise);
    switchBackend();
    fromA.reject(new Error('A went away'));
    expect(await stale).toBe(false);

    expect(slot.failed).toBe(false);
    expect(slot.loading).toBe(false);
  });

  it('after the component leaves (detach), a switch no longer touches it', async () => {
    const slot = createBackendScopedValue<Settings>('server settings');
    const detach = slot.attach();
    await slot.load(async () => settingsOf('a'));
    detach();

    switchBackend();
    expect(slot.value).toEqual(settingsOf('a'));
  });
});
