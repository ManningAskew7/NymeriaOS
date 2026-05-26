import type { AccountIdentity, AppConfig, ThemeName } from '$lib/types';
import { applyTheme } from '$lib/themes';

const STORAGE_KEY = 'nymeria-config';

// ---------------------------------------------------------------------------
// Identity-scoped localStorage helpers
// ---------------------------------------------------------------------------
// Mirror of nymeria-desktop/src/lib/stores/config.svelte.ts — keep in sync.
// User-scoped data lives under `{base_key}-{user_id}` once identity is known,
// for example `nymeria-threads-{user_id}`. Before /me returns, legacy keys are
// used so pre-Step-2 installs keep working. First /me success migrates legacy
// → scoped.

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
      showAutonomousPrompts: true,
      describeToolCalls: true,
      identity: null,
    };
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const config = JSON.parse(stored) as AppConfig;
      if (!config.theme) config.theme = 'midnight';
      if (config.suppressAttachmentWarnings === undefined) config.suppressAttachmentWarnings = false;
      if (config.showAutonomousPrompts === undefined) config.showAutonomousPrompts = true;
      if (config.describeToolCalls === undefined) config.describeToolCalls = true;
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
    showAutonomousPrompts: true,
    describeToolCalls: true,
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
  let showAutonomousPrompts = $state(initial.showAutonomousPrompts ?? true);
  let describeToolCalls = $state(initial.describeToolCalls ?? true);
  let identity = $state<AccountIdentity | null>(initial.identity ?? null);

  if (identity) {
    currentIdentityId = identity.id;
  }

  // Apply theme on initial load
  if (typeof document !== 'undefined') {
    applyTheme(theme);
  }

  function saveCurrentConfig() {
    saveConfig({
      apiUrl,
      apiKey,
      setupCompleted,
      theme,
      suppressAttachmentWarnings,
      showAutonomousPrompts,
      describeToolCalls,
      identity,
    });
  }

  function applyLoadedConfig(config: AppConfig): void {
    apiUrl = config.apiUrl;
    apiKey = config.apiKey;
    setupCompleted = config.setupCompleted ?? false;
    theme = config.theme ?? 'midnight';
    suppressAttachmentWarnings = config.suppressAttachmentWarnings ?? false;
    showAutonomousPrompts = config.showAutonomousPrompts ?? true;
    describeToolCalls = config.describeToolCalls ?? true;
    identity = config.identity ?? null;
    currentIdentityId = identity?.id ?? null;
    applyTheme(theme);
  }

  function notifyIdentityReloadHooks(label: string): void {
    for (const hook of reloadHooks) {
      try {
        hook();
      } catch (e) {
        console.error(`${label} reload hook failed:`, e);
      }
    }
  }

  function clearAuthSession(label: string): void {
    apiKey = '';
    setupCompleted = false;
    identity = null;
    currentIdentityId = null;
    saveCurrentConfig();
    notifyIdentityReloadHooks(label);
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
          // Token no longer valid. Clear the auth session immediately so the
          // root route renders SetupWizard instead of mounting app panels that
          // will all fail with 401s.
          clearAuthSession('refreshIdentity auth failure');
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

  /**
   * Mobile restores Capacitor Preferences after stores are constructed. Reload
   * the persisted config into live state, then ask scoped stores to re-read.
   */
  function reloadFromStorage(label: string = 'reloadFromStorage'): void {
    applyLoadedConfig(loadConfig());
    if (currentIdentityId) {
      migrateLegacyKeys(currentIdentityId);
    }
    notifyIdentityReloadHooks(label);
  }

  /**
   * Sign out of the current account: clears identity + apiKey and resets
   * setupCompleted so the SetupWizard renders again. Per-feature stores
   * are notified via the identity reload hooks.
   */
  function signOut(): void {
    clearAuthSession('signOut');
  }

  /**
   * Update the caller's display name via PATCH /me, then refresh the cached
   * identity. Bubbles errors so the UI can surface them to the user.
   */
  async function updateIdentityDisplayName(name: string): Promise<AccountIdentity> {
    const trimmed = name.trim();
    if (!trimmed) throw new Error('Display name cannot be empty');
    if (!apiUrl || !apiKey) throw new Error('Not connected');
    const base = apiUrl.replace(/\/$/, '');
    const response = await fetch(`${base}/me`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({ display_name: trimmed }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `Failed to update display name (${response.status})`);
    }
    const data = (await response.json()) as AccountIdentity;
    identity = data;
    currentIdentityId = data.id;
    saveCurrentConfig();
    return data;
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
      return apiUrl.trim().length > 0 && apiKey.trim().length > 0;
    },
    get needsSetup() {
      return !setupCompleted || apiUrl.trim().length === 0 || apiKey.trim().length === 0;
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
    get showAutonomousPrompts() {
      return showAutonomousPrompts;
    },
    set showAutonomousPrompts(value: boolean) {
      showAutonomousPrompts = value;
      saveCurrentConfig();
    },
    get describeToolCalls() {
      return describeToolCalls;
    },
    set describeToolCalls(value: boolean) {
      describeToolCalls = value;
      saveCurrentConfig();
    },
    get identity(): AccountIdentity | null {
      return identity;
    },
    refreshIdentity,
    clearIdentity,
    reloadFromStorage,
    signOut,
    updateIdentityDisplayName,
    reset() {
      apiUrl = '';
      apiKey = '';
      setupCompleted = false;
      theme = 'midnight';
      suppressAttachmentWarnings = false;
      showAutonomousPrompts = true;
      describeToolCalls = true;
      identity = null;
      currentIdentityId = null;
      applyTheme('midnight');
      saveCurrentConfig();
    },
  };
}

export const configStore = createConfigStore();
