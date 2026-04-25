import type { AccountIdentity, AppConfig, ThemeName } from '$lib/types';
import { applyTheme } from '$lib/themes';

const STORAGE_KEY = 'nymeria-config';

// Build-time defaults (set via VITE_DEFAULT_API_URL / VITE_DEFAULT_API_KEY env vars)
const DEFAULT_API_URL = import.meta.env.VITE_DEFAULT_API_URL || 'http://localhost:8000';
const DEFAULT_API_KEY = import.meta.env.VITE_DEFAULT_API_KEY || '';

// ---------------------------------------------------------------------------
// Identity-scoped localStorage helpers
// ---------------------------------------------------------------------------
// User-scoped data is stored under `nymeria-<user_id>-<key>` once identity is
// known. Before /me returns, the unscoped legacy keys (`nymeria-threads`,
// etc.) are still used so existing data is visible. On first /me success, a
// one-time migration copies unscoped → scoped.
//
// Keys that should NEVER be namespaced live in NON_SCOPED_KEYS: the config
// store itself (loads before identity exists) and saved-connections (picking
// which backend to talk to is pre-identity).

const NON_SCOPED_KEYS = new Set<string>([
  'nymeria-config',
  'nymeria-saved-connections',
  'nymeria-active-connection-id',
]);

// Keys that DO get migrated from unscoped → scoped on first identity resolve.
// Keep this list in sync with the `STORAGE_KEY` constants in per-feature stores.
const SCOPED_KEY_BASES = [
  'nymeria-threads',
  'nymeria-current-thread',
  'nymeria-thread-folders',
  'nymeria-thread-sort-mode',
  'nymeria-ui',
  'nymeria-ui-mobile',
];

let currentIdentityId: string | null = null;

/**
 * Return the localStorage key to use for a given base key, scoped to the
 * currently-known identity. Falls back to the base key if identity hasn't
 * been resolved yet (legacy path).
 */
export function scopedKey(base: string): string {
  if (NON_SCOPED_KEYS.has(base) || !currentIdentityId) return base;
  return `${base}-${currentIdentityId}`;
}

// ---------------------------------------------------------------------------
// Reload hook registry — stores register here so they can re-read from
// localStorage after identity changes (new scope = potentially new data).
// ---------------------------------------------------------------------------

type ReloadHook = () => void;
const reloadHooks: Set<ReloadHook> = new Set();

export function registerIdentityReloadHook(hook: ReloadHook): () => void {
  reloadHooks.add(hook);
  return () => reloadHooks.delete(hook);
}

function migrateLegacyKeys(userId: string): void {
  if (typeof localStorage === 'undefined') return;
  for (const base of SCOPED_KEY_BASES) {
    const legacy = base;
    const scoped = `${base}-${userId}`;
    const legacyValue = localStorage.getItem(legacy);
    if (legacyValue === null) continue;
    if (localStorage.getItem(scoped) !== null) {
      // Scoped already populated — don't clobber. Leave legacy alone so a
      // future identity switch can still read it (Step 7 cleans up).
      continue;
    }
    try {
      localStorage.setItem(scoped, legacyValue);
      localStorage.removeItem(legacy);
    } catch (e) {
      console.error(`Failed to migrate ${legacy} -> ${scoped}:`, e);
    }
  }
}

// ---------------------------------------------------------------------------
// Config persistence
// ---------------------------------------------------------------------------

function loadConfig(): AppConfig {
  if (typeof localStorage === 'undefined') {
    return {
      apiUrl: DEFAULT_API_URL,
      apiKey: DEFAULT_API_KEY,
      theme: 'midnight',
      suppressAttachmentWarnings: false,
      identity: null,
    };
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const config = JSON.parse(stored) as AppConfig;
      if (!config.theme) {
        config.theme = 'midnight';
      }
      if (config.suppressAttachmentWarnings === undefined) {
        config.suppressAttachmentWarnings = false;
      }
      if (config.identity === undefined) {
        config.identity = null;
      }
      return config;
    }
  } catch (e) {
    console.error('Failed to load config:', e);
  }

  return {
    apiUrl: DEFAULT_API_URL,
    apiKey: DEFAULT_API_KEY,
    theme: 'midnight',
    suppressAttachmentWarnings: false,
    identity: null,
  };
}

function saveConfig(config: AppConfig): void {
  if (typeof localStorage === 'undefined') return;

  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
  } catch (e) {
    console.error('Failed to save config:', e);
  }
}

function createConfigStore() {
  const initial = loadConfig();
  let apiUrl = $state(initial.apiUrl);
  let apiKey = $state(initial.apiKey);
  let setupCompleted = $state(initial.setupCompleted ?? false);
  let theme = $state<ThemeName>(initial.theme ?? 'midnight');
  let suppressAttachmentWarnings = $state(initial.suppressAttachmentWarnings ?? false);
  let identity = $state<AccountIdentity | null>(initial.identity ?? null);

  // Seed the module-level scope cache with whatever identity is persisted so
  // stores that load before refreshIdentity() runs still pick the right keys.
  if (identity) {
    currentIdentityId = identity.id;
  }

  // Apply theme on initial load (client-side only)
  if (typeof document !== 'undefined') {
    applyTheme(theme);
  }

  function saveCurrentConfig() {
    saveConfig({ apiUrl, apiKey, setupCompleted, theme, suppressAttachmentWarnings, identity });
  }

  /**
   * Fetch GET /me using the current apiUrl + apiKey, update identity state,
   * migrate legacy unscoped localStorage keys, and fire reload hooks so
   * per-feature stores re-read their data from the newly scoped keys.
   *
   * Returns the new identity on success, or null if the request failed
   * (missing credentials, network error, 401, etc.) — callers should treat
   * null as "route back to SetupWizard".
   */
  async function refreshIdentity(): Promise<AccountIdentity | null> {
    if (!apiUrl || !apiKey) return null;
    try {
      const base = apiUrl.replace(/\/$/, '');
      const response = await fetch(`${base}/me`, {
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${apiKey}`,
        },
      });
      if (!response.ok) {
        if (response.status === 401) {
          // Token no longer valid — clear cached identity so callers can
          // decide what to do (typically: send user back to Setup Wizard).
          identity = null;
          currentIdentityId = null;
          saveCurrentConfig();
        }
        return null;
      }
      const data = (await response.json()) as AccountIdentity;
      const previousId = currentIdentityId;
      identity = data;
      currentIdentityId = data.id;
      if (previousId !== data.id) {
        migrateLegacyKeys(data.id);
        for (const hook of reloadHooks) {
          try {
            hook();
          } catch (e) {
            console.error('identity reload hook failed:', e);
          }
        }
      }
      saveCurrentConfig();
      return data;
    } catch (e) {
      console.error('refreshIdentity failed:', e);
      return null;
    }
  }

  function clearIdentity() {
    identity = null;
    currentIdentityId = null;
    saveCurrentConfig();
  }

  return {
    get apiUrl() {
      return apiUrl;
    },
    set apiUrl(value: string) {
      apiUrl = value;
      saveCurrentConfig();
    },
    get apiKey() {
      return apiKey;
    },
    set apiKey(value: string) {
      apiKey = value;
      saveCurrentConfig();
    },
    get isConfigured() {
      return apiUrl.length > 0 && apiKey.length > 0;
    },
    get isFirstRun() {
      return !setupCompleted && !apiKey;
    },
    get setupCompleted() {
      return setupCompleted;
    },
    set setupCompleted(value: boolean) {
      setupCompleted = value;
      saveCurrentConfig();
    },
    get theme() {
      return theme;
    },
    set theme(value: ThemeName) {
      theme = value;
      applyTheme(value);
      saveCurrentConfig();
    },
    setTheme(value: ThemeName) {
      theme = value;
      applyTheme(value);
      saveCurrentConfig();
    },
    completeSetup() {
      setupCompleted = true;
      saveCurrentConfig();
    },
    get suppressAttachmentWarnings() {
      return suppressAttachmentWarnings;
    },
    set suppressAttachmentWarnings(value: boolean) {
      suppressAttachmentWarnings = value;
      saveCurrentConfig();
    },
    get identity(): AccountIdentity | null {
      return identity;
    },
    refreshIdentity,
    clearIdentity,
    reset() {
      apiUrl = DEFAULT_API_URL;
      apiKey = DEFAULT_API_KEY;
      setupCompleted = false;
      theme = 'midnight';
      suppressAttachmentWarnings = false;
      identity = null;
      currentIdentityId = null;
      applyTheme('midnight');
      saveCurrentConfig();
    }
  };
}

export const configStore = createConfigStore();
