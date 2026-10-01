import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// #242: the identity reload hooks are the reset contract for every
// backend-scoped store. They used to fire only when the ACCOUNT ID changed,
// and every backend names its owner `default`, so a switch between two
// backends reset nothing and the scoped localStorage keys collided. Real
// store, stubbed boundaries (localStorage, fetch, the OS keychain). The store
// instantiates at import, so each test re-imports it after vi.resetModules().

vi.mock('$lib/services/secureStorage', () => ({
  secureGet: vi.fn().mockResolvedValue(null),
  secureSet: vi.fn().mockResolvedValue(undefined),
  secureDelete: vi.fn().mockResolvedValue(undefined),
}));

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
  clear() {
    this.data.clear();
  }
}

const A = 'http://localhost:8097';
const B = 'http://localhost:8098';

let storage: MemoryStorage;
let fetchMock: ReturnType<typeof vi.fn>;

function identity(id: string, role = 'admin') {
  return { id, email: `${id}@localhost`, display_name: id, role };
}

function meOk(id: string, role = 'admin') {
  return new Response(JSON.stringify(identity(id, role)), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function seedConfig(apiUrl: string, accountId: string | null, apiKey = 'nym_token') {
  storage.setItem(
    'nymeria-config',
    JSON.stringify({
      apiUrl,
      apiKey,
      setupCompleted: true,
      theme: 'light',
      identity: accountId ? identity(accountId) : null,
    }),
  );
}

/** A signed-out (or first-run) blob: no token, setup not completed, no identity. */
function seedSignedOut(apiUrl: string) {
  storage.setItem(
    'nymeria-config',
    JSON.stringify({ apiUrl, apiKey: '', setupCompleted: false, theme: 'light', identity: null }),
  );
}

async function loadStore() {
  vi.resetModules();
  const mod = await import('./config.svelte');
  // The keychain is mocked empty, so the token comes from the seeded blob.
  const hooks: string[] = [];
  mod.registerIdentityReloadHook(() => hooks.push('fired'));
  return { ...mod, hooks };
}

beforeEach(() => {
  storage = new MemoryStorage();
  vi.stubGlobal('localStorage', storage);
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('refreshIdentity: when the reload hooks fire', () => {
  it('a switch to another backend whose owner is also `default` fires every hook exactly once', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, registerIdentityReloadHook } = await loadStore();
    const second: string[] = [];
    registerIdentityReloadHook(() => second.push('fired'));

    configStore.apiUrl = B;
    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity();

    expect(fetchMock).toHaveBeenCalledWith(`${B}/me`, expect.anything());
    expect(hooks).toEqual(['fired']);
    expect(second).toEqual(['fired']);
    expect(configStore.identity?.id).toBe('default');
  });

  it('a focus refresh on the same backend and account fires nothing', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, identityReloadGeneration } = await loadStore();
    const before = identityReloadGeneration();

    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity();

    expect(hooks).toEqual([]);
    expect(identityReloadGeneration()).toBe(before);
  });

  it('URL spellings of the same backend are the same scope: no hook', async () => {
    seedConfig('http://LocalHost:8097/', 'default');
    const { configStore, hooks } = await loadStore();
    configStore.apiUrl = 'http://localhost:8097';

    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity();

    expect(hooks).toEqual([]);
  });

  it('another account on the same backend fires the hooks', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks } = await loadStore();

    fetchMock.mockResolvedValueOnce(meOk('alice'));
    await configStore.refreshIdentity();

    expect(hooks).toEqual(['fired']);
    expect(configStore.identity?.id).toBe('alice');
  });

  it('forceReload fires the hooks on an unchanged scope, once', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, identityReloadGeneration } = await loadStore();
    const before = identityReloadGeneration();

    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity({ forceReload: true });

    expect(hooks).toEqual(['fired']);
    expect(identityReloadGeneration()).toBe(before + 1);
  });

  it('a 401 clears the session and fires the hooks once', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks } = await loadStore();

    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 401 }));
    const result = await configStore.refreshIdentity({ forceReload: true });

    expect(result).toBeNull();
    expect(hooks).toEqual(['fired']);
    expect(configStore.identity).toBeNull();
    expect(configStore.apiKey).toBe('');
    expect(configStore.setupCompleted).toBe(false);
  });
});

describe('refreshIdentity: an unresolved /me', () => {
  it('a network error after the backend moved drops the old scope and resets', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, currentIdentityScope, scopedKey } = await loadStore();
    expect(currentIdentityScope()?.backend).toBe(A);

    configStore.apiUrl = B;
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const result = await configStore.refreshIdentity();

    expect(result).toBeNull();
    expect(hooks).toEqual(['fired']);
    expect(configStore.identity).toBeNull();
    // Unresolved: parked on B's provisional keys, never the previous
    // backend's, and never the unscoped ones the next backend would adopt.
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: '' });
    expect(scopedKey('nymeria-threads')).toBe(`nymeria-threads-@${B}`);
    // The token survives: this is not a sign-out.
    expect(configStore.apiKey).toBe('nym_token');
  });

  it('a forced refresh that fails on the same URL also resets (a token swap must not keep the old scope)', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, currentIdentityScope } = await loadStore();

    configStore.apiKey = 'nym_other';
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 503 }));
    await configStore.refreshIdentity({ forceReload: true });

    expect(hooks).toEqual(['fired']);
    expect(currentIdentityScope()).toEqual({ backend: A, accountId: '' });
  });

  it('a network blip on the unchanged connection keeps the scope and fires nothing', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, currentIdentityScope } = await loadStore();

    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await configStore.refreshIdentity();

    expect(hooks).toEqual([]);
    expect(configStore.identity?.id).toBe('default');
    expect(currentIdentityScope()?.backend).toBe(A);
  });

  it('a /me answer for a connection that changed meanwhile is dropped', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, currentIdentityScope } = await loadStore();

    let answer: (r: Response) => void = () => undefined;
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { answer = resolve; }));
    configStore.apiUrl = B;
    const inFlight = configStore.refreshIdentity({ forceReload: true });
    configStore.apiUrl = 'http://localhost:8099';
    answer(meOk('bob'));
    expect(await inFlight).toBeNull();

    expect(hooks).toEqual([]);
    expect(configStore.identity?.id).toBe('default');
    expect(currentIdentityScope()?.backend).toBe(A);
  });
});

describe('beginConnectionSwitch: the reset before the repoint (review S-MED-1, S-LOW-4)', () => {
  it('fires every hook once, synchronously, and parks the scope on the target backend with no identity', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, currentIdentityScope, scopedKey, identityReloadGeneration } = await loadStore();
    const before = identityReloadGeneration();

    configStore.beginConnectionSwitch(`${B}/`);

    expect(hooks).toEqual(['fired']);
    expect(identityReloadGeneration()).toBe(before + 1);
    expect(configStore.identity).toBeNull();
    // The repoint is the caller's next step; nothing has asked anyone yet.
    expect(configStore.apiUrl).toBe(A);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: '' });
    expect(scopedKey('nymeria-threads')).toBe(`nymeria-threads-@${B}`);
  });

  it('the forced /me then fires the hooks again under the resolved scope and carries what was written meanwhile', async () => {
    seedConfig(A, 'default');
    const { configStore, hooks, currentIdentityScope, scopedKey } = await loadStore();
    configStore.beginConnectionSwitch(B);
    configStore.apiUrl = B;
    storage.setItem(scopedKey('nymeria-thread-sort-mode'), 'alphabetical');

    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity({ forceReload: true });

    expect(hooks).toEqual(['fired', 'fired']);
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: 'default' });
    expect(storage.getItem(scopedKey('nymeria-thread-sort-mode'))).toBe('alphabetical');
    expect(storage.getItem(`nymeria-thread-sort-mode-@${B}`)).toBeNull();
  });

  it('writes made while B`s /me failed stay B`s: a backend that resolves later inherits none of them', async () => {
    seedConfig(A, 'default');
    const { configStore, currentIdentityScope, scopedKey } = await loadStore();
    configStore.beginConnectionSwitch(B);
    configStore.apiUrl = B;
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await configStore.refreshIdentity({ forceReload: true });
    storage.setItem(scopedKey('nymeria-thread-folders'), '[{"id":"b","name":"Made on B"}]');

    const C = 'http://localhost:8099';
    configStore.beginConnectionSwitch(C);
    configStore.apiUrl = C;
    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity({ forceReload: true });

    expect(currentIdentityScope()).toEqual({ backend: C, accountId: 'default' });
    expect(storage.getItem(scopedKey('nymeria-thread-folders'))).toBeNull();
    expect(storage.getItem(`nymeria-thread-folders-@${B}`)).toBe('[{"id":"b","name":"Made on B"}]');
  });
});

describe('backend-qualified storage scope', () => {
  it('the same account on two backends gets two keys; spellings of one backend share one', async () => {
    seedConfig(A, 'default');
    const { configStore, scopedKey } = await loadStore();
    const onA = scopedKey('nymeria-threads');

    configStore.apiUrl = B;
    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity();
    const onB = scopedKey('nymeria-threads');

    expect(onA).not.toBe(onB);
    expect(onA).not.toBe('nymeria-threads-default');
    expect(scopedKey('nymeria-config')).toBe('nymeria-config');
  });

  it('localhost and 127.0.0.1 are not folded together', async () => {
    seedConfig('http://localhost:8097', 'default');
    const first = (await loadStore()).scopedKey('nymeria-threads');
    storage.clear();
    seedConfig('http://127.0.0.1:8097', 'default');
    const second = (await loadStore()).scopedKey('nymeria-threads');
    expect(first).not.toBe(second);
  });

  it('carries the account-only folders to the backend in use at upgrade, once, and never clobbers', async () => {
    storage.setItem('nymeria-thread-folders-default', '[{"id":"f1","name":"Work"}]');
    seedConfig(A, 'default');
    const { configStore, scopedKey } = await loadStore();

    // Carried at boot, before any store reads its keys.
    const keyOnA = scopedKey('nymeria-thread-folders');
    expect(storage.getItem(keyOnA)).toBe('[{"id":"f1","name":"Work"}]');
    expect(storage.getItem('nymeria-thread-folders-default')).toBeNull();

    // A first visit to another backend under the same account inherits nothing.
    configStore.apiUrl = B;
    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity({ forceReload: true });
    expect(storage.getItem(scopedKey('nymeria-thread-folders'))).toBeNull();

    // An existing scoped key is never clobbered by a stray older-format one.
    storage.setItem('nymeria-thread-folders-default', '[{"id":"f2","name":"Stray"}]');
    configStore.apiUrl = A;
    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity({ forceReload: true });
    expect(storage.getItem(keyOnA)).toBe('[{"id":"f1","name":"Work"}]');
  });

  it('the first resolve after a sign-in carries unscoped pre-identity data into the scope', async () => {
    seedSignedOut(A);
    storage.setItem('nymeria-thread-sort-mode', 'alphabetical');
    const { configStore, scopedKey } = await loadStore();
    expect(scopedKey('nymeria-thread-sort-mode')).toBe('nymeria-thread-sort-mode');

    // The sign-in surface's sequence (onboarding ConnectSection.adopt).
    configStore.apiUrl = A;
    configStore.apiKey = 'nym_token';
    configStore.completeSetup();
    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity();

    expect(storage.getItem(scopedKey('nymeria-thread-sort-mode'))).toBe('alphabetical');
    expect(storage.getItem('nymeria-thread-sort-mode')).toBeNull();
  });
});

describe('a relaunch while parked stays on the backend`s provisional scope (delta review LOW-3)', () => {
  it('boots on B`s provisional keys, keeps them through a /me blip, and carries them once B resolves', async () => {
    // Persisted by a switch whose /me never answered: B, signed in, no account.
    seedConfig(B, null);
    storage.setItem(`nymeria-thread-folders-@${B}`, '[{"id":"b","name":"Made on B"}]');
    storage.setItem('nymeria-thread-folders', '[{"id":"u","name":"Signed-out folder"}]');
    const { configStore, hooks, currentIdentityScope, scopedKey } = await loadStore();

    expect(currentIdentityScope()).toEqual({ backend: B, accountId: '' });
    expect(scopedKey('nymeria-thread-folders')).toBe(`nymeria-thread-folders-@${B}`);

    // The boot /me fails: still B's waiting room, nothing reset.
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await configStore.refreshIdentity();
    expect(hooks).toEqual([]);
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: '' });

    fetchMock.mockResolvedValueOnce(meOk('default'));
    await configStore.refreshIdentity();
    expect(currentIdentityScope()).toEqual({ backend: B, accountId: 'default' });
    expect(hooks).toEqual(['fired']);
    expect(storage.getItem(scopedKey('nymeria-thread-folders'))).toBe('[{"id":"b","name":"Made on B"}]');
    expect(storage.getItem(`nymeria-thread-folders-@${B}`)).toBeNull();
  });

  it('a signed-out blob still boots on the unscoped legacy keys', async () => {
    seedSignedOut(B);
    const { currentIdentityScope, scopedKey } = await loadStore();
    expect(currentIdentityScope()).toBeNull();
    expect(scopedKey('nymeria-thread-folders')).toBe('nymeria-thread-folders');
  });

  it('a sign-in whose /me fails parks on that backend instead of writing unscoped', async () => {
    seedSignedOut(A);
    const { configStore, hooks, currentIdentityScope, scopedKey } = await loadStore();
    configStore.apiUrl = A;
    configStore.apiKey = 'nym_token';
    configStore.completeSetup();
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 503 }));
    await configStore.refreshIdentity();

    expect(hooks).toEqual(['fired']);
    expect(currentIdentityScope()).toEqual({ backend: A, accountId: '' });
    expect(scopedKey('nymeria-thread-folders')).toBe(`nymeria-thread-folders-@${A}`);
    expect(configStore.apiKey).toBe('nym_token');
  });

  it('with no token there is nothing to park: a blank refresh leaves a signed-out session unscoped', async () => {
    seedSignedOut(A);
    const { configStore, hooks, currentIdentityScope } = await loadStore();
    await configStore.refreshIdentity();
    expect(hooks).toEqual([]);
    expect(currentIdentityScope()).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
