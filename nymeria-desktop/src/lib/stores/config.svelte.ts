import type { AccountIdentity, AppConfig, ThemeName } from '$lib/types';
import { applyTheme } from '$lib/themes';
import { secureGet, secureSet, secureDelete } from '$lib/services/secureStorage';
import { runOriginProbe, type OriginProbe } from '$lib/utils/firstRun';

const STORAGE_KEY = 'nymeria-config';
// H-7: the live bearer token is persisted in the OS keychain under this key,
// never inline in the localStorage config blob.
const CONFIG_APIKEY_KEYCHAIN_KEY = 'config-apikey';

// Build-time defaults (set via VITE_DEFAULT_API_URL / VITE_DEFAULT_API_KEY env vars)
const DEFAULT_API_URL = import.meta.env.VITE_DEFAULT_API_URL || 'http://localhost:8000';
const DEFAULT_API_KEY = import.meta.env.VITE_DEFAULT_API_KEY || '';
const BACKEND_ORIGIN_PROBE_TIMEOUT_MS = 1500;

type HealthProbeResponse = {
  status?: string;
  version?: string;
};

function isTauriRuntime(): boolean {
  // `__TAURI_INTERNALS__` is always injected by the Tauri 2 shell;
  // `__TAURI__` only with `app.withGlobalTauri`, which this app does not set
  // (secureStorage.ts relies on the same distinction).
  return (
    typeof window !== 'undefined' && ('__TAURI_INTERNALS__' in window || '__TAURI__' in window)
  );
}

async function detectBackendOrigin(): Promise<string | null> {
  if (typeof window === 'undefined' || isTauriRuntime()) return null;

  const origin = window.location.origin;
  if (!origin || origin === 'null') return null;

  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), BACKEND_ORIGIN_PROBE_TIMEOUT_MS);

  try {
    const response = await fetch(`${origin}/health`, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) return null;

    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.toLowerCase().includes('application/json')) return null;

    const health = (await response.json()) as HealthProbeResponse;
    return health.status === 'ok' ? origin : null;
  } catch {
    return null;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

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
  'nymeria-thread-team-ui',
  'nymeria-thread-organization-mode',
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
      theme: 'light',
      suppressAttachmentWarnings: false,
      showAutonomousPrompts: true,
      describeToolCalls: true,
      developerMode: false,
      identity: null,
    };
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const config = JSON.parse(stored) as AppConfig;
      if (!config.theme) {
        config.theme = 'light';
      }
      if (config.suppressAttachmentWarnings === undefined) {
        config.suppressAttachmentWarnings = false;
      }
      if (config.showAutonomousPrompts === undefined) {
        config.showAutonomousPrompts = true;
      }
      if (config.describeToolCalls === undefined) {
        config.describeToolCalls = true;
      }
      if (config.developerMode === undefined) {
        config.developerMode = false;
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
    theme: 'light',
    suppressAttachmentWarnings: false,
    showAutonomousPrompts: true,
    describeToolCalls: true,
    developerMode: false,
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
  let theme = $state<ThemeName>(initial.theme ?? 'light');
  let suppressAttachmentWarnings = $state(initial.suppressAttachmentWarnings ?? false);
  let showAutonomousPrompts = $state(initial.showAutonomousPrompts ?? true);
  let describeToolCalls = $state(initial.describeToolCalls ?? true);
  let developerMode = $state(initial.developerMode ?? false);
  let identity = $state<AccountIdentity | null>(initial.identity ?? null);

  // Seed the module-level scope cache with whatever identity is persisted so
  // stores that load before refreshIdentity() runs still pick the right keys.
  if (initial.identity) {
    currentIdentityId = initial.identity.id;
  }

  // Apply theme on initial load (client-side only)
  if (typeof document !== 'undefined') {
    applyTheme(initial.theme ?? 'light');
  }

  // Tracks the token last written to secure storage so routine config saves
  // (theme toggles, etc.) don't issue a redundant keychain write every time.
  let lastPersistedApiKey: string | null = null;

  function persistApiKeySecret() {
    if (apiKey === lastPersistedApiKey) return;
    lastPersistedApiKey = apiKey;
    if (apiKey) {
      void secureSet(CONFIG_APIKEY_KEYCHAIN_KEY, apiKey);
    } else {
      void secureDelete(CONFIG_APIKEY_KEYCHAIN_KEY);
    }
  }

  function saveCurrentConfig() {
    // H-7: persist the token to the OS keychain out-of-band and redact it from
    // the localStorage blob so a plaintext bearer never sits in localStorage.
    persistApiKeySecret();
    saveConfig({
      apiUrl,
      apiKey: '',
      setupCompleted,
      theme,
      suppressAttachmentWarnings,
      showAutonomousPrompts,
      describeToolCalls,
      developerMode,
      identity,
    });
  }

  // Load the token from secure storage after the synchronous init. Migrates a
  // legacy token that an older build stored inline in the config blob, then
  // redacts the plaintext copy. Setting apiKey here flips `isConfigured`, which
  // the +page boot effect reacts to, so a keychain-only install still
  // initializes once the async read resolves.
  async function hydrateApiKeySecret() {
    let stored: string | null = null;
    try {
      stored = await secureGet(CONFIG_APIKEY_KEYCHAIN_KEY);
    } catch (e) {
      console.error('Failed to hydrate API key from secure storage:', e);
    }
    if (stored) {
      lastPersistedApiKey = stored;
      if (stored !== apiKey) apiKey = stored;
      return;
    }
    if (apiKey) {
      // Legacy inline token (already loaded synchronously): migrate + redact.
      await secureSet(CONFIG_APIKEY_KEYCHAIN_KEY, apiKey);
      lastPersistedApiKey = apiKey;
      saveCurrentConfig();
    }
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
   * Sign out of the current account: clears identity + apiKey and resets
   * setupCompleted so the SetupWizard renders again. Active connection is
   * also unset on desktop (mobile is single-connection). Per-feature stores
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

  // Served-by-backend detection. Probed at most once per page load; the
  // outcome drives first-open routing (utils/firstRun.ts): a page served by a
  // Nymeria backend opens on the token-only sign-in instead of the install
  // hub. 'skipped' under Tauri or without a window, where the probe is
  // meaningless and the full hub is the only path.
  let originProbe = $state<OriginProbe>('skipped');
  let originProbePromise: Promise<string | null> | null = null;

  // Read-only: the probe never writes config. Boot may run it before the
  // keychain has hydrated the token, and a page served by backend A can be
  // deliberately pointed at backend B (the connection switcher), so only the
  // guarded autoDetectBackendOrigin below adopts the detected origin.
  function probeServedOrigin(): Promise<string | null> {
    if (originProbePromise) return originProbePromise;
    if (typeof window === 'undefined' || isTauriRuntime()) {
      originProbe = 'skipped';
      return Promise.resolve(null);
    }
    originProbePromise = runOriginProbe(detectBackendOrigin, (state) => {
      originProbe = state;
    });
    return originProbePromise;
  }

  async function autoDetectBackendOrigin(): Promise<string | null> {
    if (setupCompleted && apiKey.trim().length > 0) return null;

    const detected = await probeServedOrigin();
    if (!detected) return null;

    if (apiUrl !== detected) {
      apiUrl = detected;
      saveCurrentConfig();
    }
    return detected;
  }

  // Kick off async token hydration from the keychain (migrated installs start
  // with apiKey == '' until this resolves; the +page boot effect reacts to the
  // resulting isConfigured change).
  void hydrateApiKeySecret();

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
    get developerMode() {
      return developerMode;
    },
    set developerMode(value: boolean) {
      developerMode = value;
      saveCurrentConfig();
    },
    get identity(): AccountIdentity | null {
      return identity;
    },
    refreshIdentity,
    clearIdentity,
    signOut,
    updateIdentityDisplayName,
    autoDetectBackendOrigin,
    get originProbe(): OriginProbe {
      return originProbe;
    },
    probeServedOrigin,
    reset() {
      apiUrl = DEFAULT_API_URL;
      apiKey = DEFAULT_API_KEY;
      setupCompleted = false;
      theme = 'light';
      suppressAttachmentWarnings = false;
      showAutonomousPrompts = true;
      describeToolCalls = true;
      developerMode = false;
      identity = null;
      currentIdentityId = null;
      originProbe = 'skipped';
      originProbePromise = null;
      applyTheme('light');
      saveCurrentConfig();
    }
  };
}

export const configStore = createConfigStore();
