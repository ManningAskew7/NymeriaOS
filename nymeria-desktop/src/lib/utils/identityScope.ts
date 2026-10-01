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
 * Move the first present `sources` key to `target`, only when `target` is
 * still absent. A MOVE, not a copy, so data carried into one scope is never
 * inherited a second time by another. Returns the key moved, or null.
 */
export function moveKeyIfAbsent(
  storage: ScopeStorage,
  sources: readonly string[],
  target: string
): string | null {
  if (storage.getItem(target) !== null) return null;
  for (const source of sources) {
    if (source === target) continue;
    const value = storage.getItem(source);
    if (value === null) continue;
    try {
      storage.setItem(target, value);
      storage.removeItem(source);
      return source;
    } catch (e) {
      console.error(`Failed to carry ${source} forward to ${target}:`, e);
      return null;
    }
  }
  return null;
}

/**
 * Carry one key family's older-format data into `scope`'s key, once: the
 * account-only key (`${base}-${accountId}`, the format before #242), else the
 * unscoped base (before identity scoping). Whichever backend resolves first
 * under the new format inherits it; a later first visit to another backend
 * with the same account id starts empty.
 */
export function carryForwardScopedKey(
  storage: ScopeStorage,
  base: string,
  scope: IdentityScope
): string | null {
  return moveKeyIfAbsent(storage, [`${base}-${scope.accountId}`, base], scopedStorageKey(base, scope));
}
