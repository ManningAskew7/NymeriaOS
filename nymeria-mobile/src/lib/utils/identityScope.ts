/**
 * Backend identity scope: which backend and account the client's cached state
 * belongs to (#242).
 *
 * Every slim and Docker bootstrap names its owner account `default`, so an
 * account id alone collides across backends: switching from one server's
 * `default` to another's used to keep the first server's caches and share its
 * per-identity localStorage. The scope is the normalized backend URL PLUS the
 * account id, the same pair the saved-connections store matches on.
 *
 * Pure functions only. The config store owns the live scope and fires the
 * identity reload hooks when it changes; this file is shared byte-for-byte by
 * desktop and mobile (drift gate EXACT_MATCH) so the key format cannot drift
 * between the apps.
 */

export interface IdentityScope {
  /** Normalized backend base URL (see normalizeBackendUrl). */
  readonly backend: string;
  /** Account id from GET /me. */
  readonly accountId: string;
}

/** The slice of the Storage API the carry-forward needs (localStorage or a test double). */
export interface ScopeStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * Canonical form of a backend base URL: trimmed, no trailing slashes, scheme
 * and host lowercased, default port dropped, port and path kept. Hosts are not
 * resolved, so `localhost` and `127.0.0.1` stay distinct (a separate cache for
 * the same server is harmless; a shared one for two servers is the bug). Text
 * that is not an absolute http(s) URL is only trimmed.
 */
export function normalizeBackendUrl(url: string): string {
  const trimmed = url.trim().replace(/\/+$/, '');
  if (!trimmed) return '';
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return `${parsed.protocol}//${parsed.host}${parsed.pathname.replace(/\/+$/, '')}`;
    }
  } catch {
    // Not an absolute URL: fall through to the trimmed text.
  }
  return trimmed;
}

export function identityScope(apiUrl: string, accountId: string): IdentityScope {
  return { backend: normalizeBackendUrl(apiUrl), accountId };
}

/**
 * The scope a connection switch parks on until GET /me names the account:
 * the backend alone (account id ''). Storage written in that window stays
 * partitioned per backend instead of landing on the unscoped base keys,
 * which whichever backend resolved next used to adopt, and the first account
 * that resolves on the same backend carries it forward.
 */
export function provisionalScope(apiUrl: string): IdentityScope {
  return identityScope(apiUrl, '');
}

/**
 * The scope a persisted config resumes on (boot, and mobile's Preferences
 * restore): the resolved backend + account; for a signed-in config with no
 * account (relaunched while a switch was parked on /me), that backend's
 * provisional scope, so writes keep landing on its waiting key; null only
 * when signed out or with no backend, the unscoped legacy keys.
 */
export function resumedScope(
  apiUrl: string,
  accountId: string | null | undefined,
  signedIn: boolean
): IdentityScope | null {
  if (!apiUrl.trim()) return null;
  if (accountId) return identityScope(apiUrl, accountId);
  return signedIn ? provisionalScope(apiUrl) : null;
}

export function isProvisionalScope(scope: IdentityScope): boolean {
  return scope.accountId === '';
}

export function sameIdentityScope(a: IdentityScope | null, b: IdentityScope | null): boolean {
  if (a === null || b === null) return a === b;
  return a.backend === b.backend && a.accountId === b.accountId;
}

/** The scope as one string, `accountId@backend`: a storage-key suffix or cache key. */
export function scopeLabel(scope: IdentityScope): string {
  return `${scope.accountId}@${scope.backend}`;
}

/** The localStorage key for `base` under `scope`; the bare base before identity resolves. */
export function scopedStorageKey(base: string, scope: IdentityScope | null): string {
  return scope ? `${base}-${scopeLabel(scope)}` : base;
}

/**
 * Settle older-format `sources` into `target`, once. While `target` is absent
 * the first present source MOVES into it (a move, not a copy, so data carried
 * into one scope is never inherited a second time by another). Once `target`
 * holds data, every source still present is stale and is dropped: left
 * behind, it would be adopted by the next scope that resolves without a key
 * of its own. A failed write (quota) leaves every key where it was. `sources`
 * must not include `target`. Returns the key moved, or null.
 */
export function carryForwardKeys(
  storage: ScopeStorage,
  sources: readonly string[],
  target: string
): string | null {
  let moved: string | null = null;
  if (storage.getItem(target) === null) {
    for (const source of sources) {
      const value = storage.getItem(source);
      if (value === null) continue;
      try {
        storage.setItem(target, value);
      } catch (e) {
        console.error(`Failed to carry ${source} forward to ${target}:`, e);
        return null;
      }
      moved = source;
      break;
    }
    if (moved === null) return null;
  }
  for (const source of sources) storage.removeItem(source);
  return moved;
}

/**
 * Carry one key family's older-format data into `scope`'s key, once: the
 * same backend's provisional key (written while a switch waited for /me),
 * else the account-only key (`${base}-${accountId}`, the format before
 * #242), else the unscoped base (before identity scoping). Whichever backend
 * resolves first under the new format inherits the account-only and unscoped
 * data; a later first visit to another backend with the same account id
 * starts empty. A provisional scope is a waiting room, never a target.
 */
export function carryForwardScopedKey(
  storage: ScopeStorage,
  base: string,
  scope: IdentityScope
): string | null {
  if (isProvisionalScope(scope)) return null;
  const waiting = scopedStorageKey(base, { backend: scope.backend, accountId: '' });
  return carryForwardKeys(
    storage,
    [waiting, `${base}-${scope.accountId}`, base],
    scopedStorageKey(base, scope)
  );
}
