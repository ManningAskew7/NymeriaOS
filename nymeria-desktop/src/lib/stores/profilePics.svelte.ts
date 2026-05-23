/**
 * Per-account profile picture store. Pictures are persisted as base64 data
 * URLs in localStorage keyed by account id, so the same identity displays
 * the same picture across sessions. Frontend-only (no backend sync).
 *
 * Use:
 *   import { profilePics } from '$lib/stores/profilePics.svelte';
 *   const dataUrl = profilePics.get(identity.id);   // string | null
 *   profilePics.set(identity.id, dataUrl);          // upload
 *   profilePics.clear(identity.id);                 // revert to default
 */

const STORAGE_PREFIX = 'nymeria_profile_pic_';

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

function createStore() {
  // $state.raw because the values can be large base64 strings — no deep
  // reactivity needed, we re-assign the whole map on writes.
  let pics = $state<Record<string, string>>(loadAll());

  return {
    get(id: string | null | undefined): string | null {
      if (!id) return null;
      return pics[id] ?? null;
    },
    set(id: string, dataUrl: string): void {
      pics = { ...pics, [id]: dataUrl };
      if (typeof localStorage !== 'undefined') {
        try {
          localStorage.setItem(STORAGE_PREFIX + id, dataUrl);
        } catch (e) {
          // Quota exceeded — the image is too large. Surface to the caller.
          throw new Error(
            'Could not save profile picture (browser storage is full). Try a smaller image.'
          );
        }
      }
    },
    clear(id: string): void {
      const next = { ...pics };
      delete next[id];
      pics = next;
      if (typeof localStorage !== 'undefined') {
        localStorage.removeItem(STORAGE_PREFIX + id);
      }
    },
  };
}

export const profilePics = createStore();
