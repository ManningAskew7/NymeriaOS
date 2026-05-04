/**
 * App lifecycle management for Capacitor.
 *
 * Handles foreground/background transitions, Android back button,
 * and localStorage backup to Capacitor Preferences.
 */

import type { PluginListenerHandle } from '@capacitor/core';

let appStateListener: PluginListenerHandle | null = null;
let backButtonListener: PluginListenerHandle | null = null;

type AppStateCallback = (isActive: boolean) => void;
type BackButtonCallback = () => void;

let onAppStateChange: AppStateCallback | null = null;
let onBackButton: BackButtonCallback | null = null;

const EXACT_PREFERENCE_BACKUP_KEYS = ['nymeria-config'] as const;
const SCOPED_PREFERENCE_BACKUP_BASES = [
  'nymeria-threads',
  'nymeria-current-thread',
  'nymeria-thread-folders',
  'nymeria-thread-sort-mode',
  'nymeria-ui-mobile',
] as const;

type PreferenceKeyReader = {
  keys(): Promise<{ keys: string[] }>;
};

function isPreferenceBackupKey(key: string): boolean {
  return (
    EXACT_PREFERENCE_BACKUP_KEYS.includes(key as (typeof EXACT_PREFERENCE_BACKUP_KEYS)[number]) ||
    SCOPED_PREFERENCE_BACKUP_BASES.some((base) => key === base || key.startsWith(`${base}-`))
  );
}

function localStorageKeysToBackup(): string[] {
  if (typeof localStorage === 'undefined') return [];

  const keys = new Set<string>();
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key && isPreferenceBackupKey(key)) {
      keys.add(key);
    }
  }
  return Array.from(keys).sort();
}

async function preferenceKeysToRestore(Preferences: PreferenceKeyReader): Promise<string[]> {
  const keys = new Set<string>([...EXACT_PREFERENCE_BACKUP_KEYS, ...SCOPED_PREFERENCE_BACKUP_BASES]);

  const { keys: storedKeys } = await Preferences.keys();
  for (const key of storedKeys) {
    if (isPreferenceBackupKey(key)) {
      keys.add(key);
    }
  }

  return Array.from(keys).sort();
}

/**
 * Initialize Capacitor app lifecycle listeners.
 * Safe to call in browser (no-ops if plugins unavailable).
 */
export async function initLifecycle(callbacks: {
  onAppStateChange?: AppStateCallback;
  onBackButton?: BackButtonCallback;
}): Promise<void> {
  onAppStateChange = callbacks.onAppStateChange ?? null;
  onBackButton = callbacks.onBackButton ?? null;

  try {
    const { App } = await import('@capacitor/app');

    // App state (foreground/background)
    appStateListener = await App.addListener('appStateChange', ({ isActive }) => {
      onAppStateChange?.(isActive);
    });

    // Android back button
    backButtonListener = await App.addListener('backButton', ({ canGoBack }) => {
      if (onBackButton) {
        onBackButton();
      } else if (!canGoBack) {
        App.exitApp();
      }
    });
  } catch {
    // Running in browser, not Capacitor — lifecycle events not available
    console.log('[Lifecycle] Capacitor App plugin not available (browser mode)');
  }
}

/**
 * Clean up all lifecycle listeners.
 */
export async function destroyLifecycle(): Promise<void> {
  await appStateListener?.remove();
  await backButtonListener?.remove();
  appStateListener = null;
  backButtonListener = null;
  onAppStateChange = null;
  onBackButton = null;
}

/**
 * Sync critical localStorage keys to Capacitor Preferences (backup).
 * Call on app pause to ensure data survives WebView cache clears.
 */
export async function backupToPreferences(): Promise<void> {
  try {
    const { Preferences } = await import('@capacitor/preferences');

    for (const key of localStorageKeysToBackup()) {
      const value = localStorage.getItem(key);
      if (value !== null) {
        await Preferences.set({ key, value });
      }
    }
  } catch {
    // Preferences plugin not available
  }
}

/**
 * Restore missing localStorage keys from Capacitor Preferences.
 * Returns true when at least one key was restored.
 */
export async function restoreFromPreferences(): Promise<boolean> {
  try {
    const { Preferences } = await import('@capacitor/preferences');

    if (typeof localStorage === 'undefined') return false;

    let restored = false;
    for (const key of await preferenceKeysToRestore(Preferences)) {
      if (localStorage.getItem(key) === null) {
        const { value } = await Preferences.get({ key });
        if (value !== null) {
          localStorage.setItem(key, value);
          restored = true;
        }
      }
    }
    return restored;
  } catch {
    // Preferences plugin not available
    return false;
  }
}
