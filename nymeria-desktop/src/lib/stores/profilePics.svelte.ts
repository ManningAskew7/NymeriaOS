/**
 * Per-account profile picture store. Pictures are persisted as base64 data
 * URLs in localStorage keyed by backend + account id (the identity scope,
 * `utils/identityScope.ts`), so the same identity displays the same picture
 * across sessions, and another backend's `default` owner never shows this
 * one's picture (#242). Frontend-only (no backend sync).
 *
 * Use:
 *   import { profilePics } from '$lib/stores/profilePics.svelte';
 *   const dataUrl = profilePics.get(identity.id);   // string | null, current backend
 *   profilePics.get(entry.identity.id, entry.apiUrl); // a saved connection's backend
 *   profilePics.set(identity.id, dataUrl);          // upload
 *   profilePics.clear(identity.id);                 // revert to default
 */

import { carryForwardKeys, identityScope, isProvisionalScope, scopeLabel } from '$lib/utils/identityScope';
import { configStore, currentIdentityScope, registerIdentityReloadHook } from './config.svelte';

const STORAGE_PREFIX = 'nymeria_profile_pic_';

function picKey(accountId: string, backendUrl: string): string {
  return scopeLabel(identityScope(backendUrl, accountId));
}

function loadAll(): Record<string, string> {
  if (typeof localStorage === 'undefined') return {};
  const out: Record<string, string> = {};
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.startsWith(STORAGE_PREFIX)) {
      const id = key.slice(STORAGE_PREFIX.length);
      const value = localStorage.getItem(key);
      if (value) out[id] = value;
    }
  }
  return out;
}

// A picture saved before pictures were backend-scoped is keyed by the bare
// account id: it moves to the first backend that resolves that account,
// the same once-only carry-forward the scoped localStorage keys get (a
// switch still waiting for /me has no account to carry it to).
function carryForwardCurrentScope(): void {
  const scope = currentIdentityScope();
  if (!scope || isProvisionalScope(scope) || typeof localStorage === 'undefined') return;
  carryForwardKeys(localStorage, [STORAGE_PREFIX + scope.accountId], STORAGE_PREFIX + scopeLabel(scope));
}

function createStore() {
  carryForwardCurrentScope();
  // The values can be large base64 strings: no deep reactivity needed, we
  // re-assign the whole map on writes.
  let pics = $state<Record<string, string>>(loadAll());

  registerIdentityReloadHook(() => {
    carryForwardCurrentScope();
    pics = loadAll();
  });

  return {
    get(id: string | null | undefined, backendUrl: string = configStore.apiUrl): string | null {
      if (!id) return null;
      return pics[picKey(id, backendUrl)] ?? null;
    },
    set(id: string, dataUrl: string, backendUrl: string = configStore.apiUrl): void {
      const key = picKey(id, backendUrl);
      pics = { ...pics, [key]: dataUrl };
      if (typeof localStorage !== 'undefined') {
        try {
          localStorage.setItem(STORAGE_PREFIX + key, dataUrl);
        } catch (e) {
          // Quota exceeded: the image is too large. Surface to the caller.
          throw new Error(
            'Could not save profile picture (browser storage is full). Try a smaller image.'
          );
        }
      }
    },
    clear(id: string, backendUrl: string = configStore.apiUrl): void {
      const key = picKey(id, backendUrl);
      const next = { ...pics };
      delete next[key];
      pics = next;
      if (typeof localStorage !== 'undefined') {
        localStorage.removeItem(STORAGE_PREFIX + key);
      }
    },
  };
}

export const profilePics = createStore();
