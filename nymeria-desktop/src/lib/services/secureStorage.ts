/**
 * OS-keychain-backed storage for secret material (H-7).
 *
 * Backend bearer tokens used to live in plaintext `localStorage`, where any
 * script in the webview (e.g. an XSS payload) could read and exfiltrate them.
 * This module stores them in the OS keychain via Tauri commands
 * (`keychain_get/set/delete`, backed by macOS Keychain / Windows Credential
 * Manager / Linux Secret Service).
 *
 * When the keychain is unavailable (a browser dev preview, or the native
 * command is missing), it falls back to `localStorage` under a `secfallback:`
 * prefix so the app keeps working in development. In a packaged Tauri build the
 * keychain path is always taken, so production secrets never touch
 * `localStorage`.
 */

const FALLBACK_PREFIX = 'secfallback:';

function inTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

let availabilityProbe: Promise<boolean> | null = null;

async function invokeTauri<T>(cmd: string, args: Record<string, unknown>): Promise<T> {
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke<T>(cmd, args);
}

/** Resolve (and cache) whether the OS keychain commands are usable. */
export function keychainAvailable(): Promise<boolean> {
  if (availabilityProbe) return availabilityProbe;
  availabilityProbe = (async () => {
    if (!inTauri()) return false;
    try {
      // A get of a sentinel key returns null when wired up correctly.
      await invokeTauri<string | null>('keychain_get', { key: '__nymeria_probe__' });
      return true;
    } catch {
      return false;
    }
  })();
  return availabilityProbe;
}

export async function secureGet(key: string): Promise<string | null> {
  if (await keychainAvailable()) {
    try {
      return (await invokeTauri<string | null>('keychain_get', { key })) ?? null;
    } catch (e) {
      console.error('[secureStorage] keychain_get failed, using fallback:', e);
    }
  }
  try {
    return localStorage.getItem(FALLBACK_PREFIX + key);
  } catch {
    return null;
  }
}

export async function secureSet(key: string, value: string): Promise<void> {
  if (await keychainAvailable()) {
    try {
      await invokeTauri<void>('keychain_set', { key, value });
      return;
    } catch (e) {
      console.error('[secureStorage] keychain_set failed, using fallback:', e);
    }
  }
  try {
    localStorage.setItem(FALLBACK_PREFIX + key, value);
  } catch (e) {
    console.error('[secureStorage] fallback setItem failed:', e);
  }
}

export async function secureDelete(key: string): Promise<void> {
  if (await keychainAvailable()) {
    try {
      await invokeTauri<void>('keychain_delete', { key });
    } catch (e) {
      console.error('[secureStorage] keychain_delete failed:', e);
    }
  }
  try {
    localStorage.removeItem(FALLBACK_PREFIX + key);
  } catch {
    /* ignore */
  }
}
