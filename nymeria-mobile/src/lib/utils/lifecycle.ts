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
    const keysToBackup = ['nymeria-config', 'nymeria-threads', 'nymeria-current-thread', 'nymeria-ui-mobile'];

    for (const key of keysToBackup) {
      const value = localStorage.getItem(key);
      if (value) {
        await Preferences.set({ key, value });
      }
    }
  } catch {
    // Preferences plugin not available
  }
}

/**
 * Restore localStorage from Capacitor Preferences (if localStorage is empty).
 * Call on app startup before stores initialize.
 */
export async function restoreFromPreferences(): Promise<void> {
  try {
    const { Preferences } = await import('@capacitor/preferences');
    const keysToRestore = ['nymeria-config', 'nymeria-threads', 'nymeria-current-thread', 'nymeria-ui-mobile'];

    for (const key of keysToRestore) {
      if (!localStorage.getItem(key)) {
        const { value } = await Preferences.get({ key });
        if (value) {
          localStorage.setItem(key, value);
        }
      }
    }
  } catch {
    // Preferences plugin not available
  }
}
