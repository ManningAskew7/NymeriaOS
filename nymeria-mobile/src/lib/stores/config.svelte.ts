import type { AccountIdentity, AppConfig, ThemeName } from '$lib/types';
import { applyTheme } from '$lib/themes';

const STORAGE_KEY = 'nymeria-config';

// ---------------------------------------------------------------------------
// Identity-scoped localStorage helpers
// ---------------------------------------------------------------------------
// Mirror of nymeria-desktop/src/lib/stores/config.svelte.ts — keep in sync.
// User-scoped data lives under `nymeria-<user_id>-<key>` once identity is
// known. Before /me returns, unscoped legacy keys are still used so
// pre-Step-2 installs keep working. First /me success migrates legacy → scoped.

const NON_SCOPED_KEYS = new Set<string>([
  'nymeria-config',
  'nymeria-saved-connections',
  'nymeria-active-connection-id',
]);

const SCOPED_KEY_BASES = [
  'nymeria-threads',
  'nymeria-current-thread',
  'nymeria-thread-folders',
  'nymeria-thread-sort-mode',
  'nymeria-ui',
  'nymeria-ui-mobile',
];

let currentIdentityId: string | null = null;

export function scopedKey(base: string): string {
  if (NON_SCOPED_KEYS.has(base) || !currentIdentityId) return base;
  return `${base}-${currentIdentityId}`;
}

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
    if (localStorage.getItem(scoped) !== null) continue;
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
      apiUrl: '',
      apiKey: '',
      theme: 'midnight',
      suppressAttachmentWarnings: false,
      identity: null,
    };
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const config = JSON.parse(stored) as AppConfig;
      if (!config.theme) config.theme = 'midnight';
      if (config.suppressAttachmentWarnings === undefined) config.suppressAttachmentWarnings = false;
      if (config.identity === undefined) config.identity = null;
      return config;
    }
  } catch (e) {
    console.error('Failed to load config:', e);
  }

  // Mobile default: empty URL (user must configure network address)
  return {
    apiUrl: '',
    apiKey: '',
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

  if (identity) {
    currentIdentityId = identity.id;
  }

  // Apply theme on initial load
  if (typeof document !== 'undefined') {
    applyTheme(theme);
  }

  function saveCurrentConfig() {
    saveConfig({ apiUrl, apiKey, setupCompleted, theme, suppressAttachmentWarnings, identity });
  }

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
      apiUrl = '';
      apiKey = '';
      setupCompleted = false;
      theme = 'midnight';
      suppressAttachmentWarnings = false;
      identity = null;
      currentIdentityId = null;
      applyTheme('midnight');
      saveCurrentConfig();
    },
  };
}

export const configStore = createConfigStore();
