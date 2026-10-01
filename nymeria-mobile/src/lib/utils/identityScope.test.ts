import { describe, expect, it } from 'vitest';
import {
  carryForwardKeys,
  carryForwardScopedKey,
  identityScope,
  isProvisionalScope,
  normalizeBackendUrl,
  provisionalScope,
  resumedScope,
  sameIdentityScope,
  scopedStorageKey,
  type ScopeStorage,
} from './identityScope';

// #242: client caches are keyed on backend URL + account id, because every
// backend names its owner `default`. Shared byte-for-byte by both apps.

function memoryStorage(seed: Record<string, string> = {}): ScopeStorage & { data: Map<string, string> } {
  const data = new Map(Object.entries(seed));
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  };
}

const A = 'http://localhost:8097';
const B = 'http://localhost:8098';

describe('normalizeBackendUrl', () => {
  it('folds case, trailing slashes and the default port, keeping port and path', () => {
    expect(normalizeBackendUrl('http://Host:8000/')).toBe('http://host:8000');
    expect(normalizeBackendUrl('  HTTP://HOST:8000//  ')).toBe('http://host:8000');
    expect(normalizeBackendUrl('https://example.com:443/nymeria/')).toBe('https://example.com/nymeria');
    expect(normalizeBackendUrl('https://example.com/Nymeria')).toBe('https://example.com/Nymeria');
  });

  it('keeps distinct servers distinct: hosts are never resolved, ports and paths count', () => {
    expect(normalizeBackendUrl('http://localhost:8000')).not.toBe(normalizeBackendUrl('http://127.0.0.1:8000'));
    expect(normalizeBackendUrl(A)).not.toBe(normalizeBackendUrl(B));
    expect(normalizeBackendUrl('https://h.example/a')).not.toBe(normalizeBackendUrl('https://h.example/b'));
  });

  it('empty, blank and non-URL text: blank stays empty, other text is only trimmed', () => {
    expect(normalizeBackendUrl('')).toBe('');
    expect(normalizeBackendUrl('   ')).toBe('');
    expect(normalizeBackendUrl(' localhost:8000/ ')).toBe('localhost:8000');
  });
});

describe('scope identity and keys', () => {
  it('the same account id on two backends is two scopes with two keys', () => {
    const onA = identityScope(A, 'default');
    const onB = identityScope(B, 'default');
    expect(sameIdentityScope(onA, onB)).toBe(false);
    expect(scopedStorageKey('nymeria-threads', onA)).not.toBe(scopedStorageKey('nymeria-threads', onB));
  });

  it('URL spellings of one backend are one scope with one key', () => {
    const one = identityScope('http://Host:8000/', 'default');
    const two = identityScope('http://host:8000', 'default');
    expect(sameIdentityScope(one, two)).toBe(true);
    expect(scopedStorageKey('nymeria-threads', one)).toBe(scopedStorageKey('nymeria-threads', two));
  });

  it('a different account on the same backend is a different scope', () => {
    expect(sameIdentityScope(identityScope(A, 'default'), identityScope(A, 'alice'))).toBe(false);
  });

  it('null scopes: equal only to each other, and the key falls back to the bare base', () => {
    expect(sameIdentityScope(null, null)).toBe(true);
    expect(sameIdentityScope(null, identityScope(A, 'default'))).toBe(false);
    expect(sameIdentityScope(identityScope(A, 'default'), null)).toBe(false);
    expect(scopedStorageKey('nymeria-threads', null)).toBe('nymeria-threads');
  });

  it('a provisional scope (backend known, account not yet) is its own key per backend', () => {
    const waitingOnA = provisionalScope('http://LOCALHOST:8097/');
    expect(waitingOnA).toEqual({ backend: A, accountId: '' });
    expect(isProvisionalScope(waitingOnA)).toBe(true);
    expect(isProvisionalScope(identityScope(A, 'default'))).toBe(false);
    const key = scopedStorageKey('nymeria-threads', waitingOnA);
    expect(key).not.toBe('nymeria-threads');
    expect(key).not.toBe(scopedStorageKey('nymeria-threads', identityScope(A, 'default')));
    expect(key).not.toBe(scopedStorageKey('nymeria-threads', provisionalScope(B)));
  });

  it('the scoped key never equals the account-only key it replaces', () => {
    const key = scopedStorageKey('nymeria-thread-folders', identityScope(A, 'default'));
    expect(key).not.toBe('nymeria-thread-folders-default');
    expect(key.startsWith('nymeria-thread-folders-')).toBe(true);
  });
});

describe('resumedScope: the scope a persisted config boots on (delta review LOW-3)', () => {
  it('a resolved account resumes its own backend + account scope', () => {
    expect(resumedScope('http://LocalHost:8097/', 'default', true)).toEqual({ backend: A, accountId: 'default' });
    expect(resumedScope(A, 'default', false)).toEqual({ backend: A, accountId: 'default' });
  });

  it('signed in with no account (relaunched while a switch was parked) resumes the provisional scope', () => {
    expect(resumedScope(`${B}/`, null, true)).toEqual({ backend: B, accountId: '' });
    expect(resumedScope(B, undefined, true)).toEqual({ backend: B, accountId: '' });
    expect(resumedScope(B, '', true)).toEqual({ backend: B, accountId: '' });
  });

  it('signed out, or no backend at all, is unscoped: the legacy keys', () => {
    expect(resumedScope(B, null, false)).toBeNull();
    expect(resumedScope('', null, true)).toBeNull();
    expect(resumedScope('   ', 'default', true)).toBeNull();
  });
});

describe('carryForwardScopedKey', () => {
  const BASE = 'nymeria-thread-folders';

  it('moves the account-only key to the first backend that resolves, and a later backend inherits nothing', () => {
    const storage = memoryStorage({ [`${BASE}-default`]: '["work"]' });
    const onA = identityScope(A, 'default');
    const onB = identityScope(B, 'default');

    expect(carryForwardScopedKey(storage, BASE, onA)).toBe(`${BASE}-default`);
    expect(storage.getItem(scopedStorageKey(BASE, onA))).toBe('["work"]');
    expect(storage.getItem(`${BASE}-default`)).toBeNull();

    expect(carryForwardScopedKey(storage, BASE, onB)).toBeNull();
    expect(storage.getItem(scopedStorageKey(BASE, onB))).toBeNull();
  });

  it('never clobbers an existing scoped key, and drops the stale older-format keys beside it', () => {
    // Left behind (a downgrade then upgrade, the dogfood shape), `-default`
    // would be adopted by the next new backend that resolves `default`.
    const onA = identityScope(A, 'default');
    const storage = memoryStorage({
      [scopedStorageKey(BASE, onA)]: '["mine"]',
      [`${BASE}-default`]: '["old"]',
      [BASE]: '["unscoped"]',
    });
    expect(carryForwardScopedKey(storage, BASE, onA)).toBeNull();
    expect(storage.getItem(scopedStorageKey(BASE, onA))).toBe('["mine"]');
    expect(storage.getItem(`${BASE}-default`)).toBeNull();
    expect(storage.getItem(BASE)).toBeNull();
    expect(carryForwardScopedKey(storage, BASE, identityScope(B, 'default'))).toBeNull();
    expect(storage.getItem(scopedStorageKey(BASE, identityScope(B, 'default')))).toBeNull();
  });

  it('prefers the account-only key over the unscoped base, and falls back to the base', () => {
    const onA = identityScope(A, 'default');
    const both = memoryStorage({ [`${BASE}-default`]: 'account', [BASE]: 'unscoped' });
    carryForwardScopedKey(both, BASE, onA);
    expect(both.getItem(scopedStorageKey(BASE, onA))).toBe('account');
    // The unscoped leftover is stale once the scope holds data.
    expect(both.getItem(BASE)).toBeNull();

    const baseOnly = memoryStorage({ [BASE]: 'unscoped' });
    expect(carryForwardScopedKey(baseOnly, BASE, onA)).toBe(BASE);
    expect(baseOnly.getItem(scopedStorageKey(BASE, onA))).toBe('unscoped');
    expect(baseOnly.getItem(BASE)).toBeNull();
  });

  it('only the matching account id carries: another account starts empty', () => {
    const storage = memoryStorage({ [`${BASE}-default`]: '["work"]' });
    expect(carryForwardScopedKey(storage, BASE, identityScope(A, 'alice'))).toBeNull();
    expect(storage.getItem(`${BASE}-default`)).toBe('["work"]');
  });

  it('what a switch wrote while /me was unresolved carries to the account that then resolves on that backend', () => {
    const waiting = scopedStorageKey(BASE, provisionalScope(B));
    const storage = memoryStorage({
      [waiting]: '["made while B was answering"]',
      [`${BASE}-default`]: '["pre-#242"]',
    });
    expect(carryForwardScopedKey(storage, BASE, identityScope(A, 'default'))).toBe(`${BASE}-default`);
    expect(storage.getItem(waiting)).toBe('["made while B was answering"]');

    expect(carryForwardScopedKey(storage, BASE, identityScope(B, 'default'))).toBe(waiting);
    expect(storage.getItem(scopedStorageKey(BASE, identityScope(B, 'default')))).toBe(
      '["made while B was answering"]'
    );
    expect(storage.getItem(waiting)).toBeNull();
  });

  it('a provisional scope is never a target: nothing moves into it and nothing is dropped', () => {
    const waiting = provisionalScope(A);
    const storage = memoryStorage({
      [scopedStorageKey(BASE, waiting)]: 'waiting',
      [BASE]: 'unscoped',
    });
    expect(carryForwardScopedKey(storage, BASE, waiting)).toBeNull();
    expect(storage.getItem(scopedStorageKey(BASE, waiting))).toBe('waiting');
    expect(storage.getItem(BASE)).toBe('unscoped');
  });

  it('an empty string is data, not absence', () => {
    const onA = identityScope(A, 'default');
    const storage = memoryStorage({ [`${BASE}-default`]: '' });
    expect(carryForwardScopedKey(storage, BASE, onA)).toBe(`${BASE}-default`);
    expect(storage.getItem(scopedStorageKey(BASE, onA))).toBe('');
  });
});

describe('carryForwardKeys', () => {
  it('keeps every key where it was when the write throws (quota), and reports nothing moved', () => {
    const storage = memoryStorage({ old: 'v', older: 'w' });
    const throwing: ScopeStorage = {
      ...storage,
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
    };
    expect(carryForwardKeys(throwing, ['old', 'older'], 'new')).toBeNull();
    expect(storage.getItem('old')).toBe('v');
    expect(storage.getItem('older')).toBe('w');
    expect(storage.getItem('new')).toBeNull();
  });

  it('with no source present and no target, it touches nothing', () => {
    const storage = memoryStorage({ unrelated: 'u' });
    expect(carryForwardKeys(storage, ['old'], 'new')).toBeNull();
    expect([...storage.data.keys()]).toEqual(['unrelated']);
  });

  it('moves the first present source and drops the later ones', () => {
    const storage = memoryStorage({ second: '2', third: '3' });
    expect(carryForwardKeys(storage, ['first', 'second', 'third'], 'target')).toBe('second');
    expect(storage.getItem('target')).toBe('2');
    expect(storage.getItem('second')).toBeNull();
    expect(storage.getItem('third')).toBeNull();
  });
});
