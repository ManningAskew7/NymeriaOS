import type { AccountIdentity, AppConfig, ThemeName } from '$lib/types';
import { applyTheme } from '$lib/themes';
import { secureGet, secureSet, secureDelete } from '$lib/services/secureStorage';
import {
  readServerConfigured,
  runOriginProbe,
  shouldProbeStoredServer,
  type OriginProbe
} from '$lib/utils/firstRun';
import {
  carryForwardScopedKey,
  identityScope,
  normalizeBackendUrl,
  sameIdentityScope,
  scopedStorageKey,
  type IdentityScope
} from '$lib/utils/identityScope';

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
// Per-identity data is stored under `<base>-<account_id>@<backend_url>` once
// GET /me has resolved (`utils/identityScope.ts` owns the format, #242). The
// scope is the BACKEND plus the account, not the account alone: every slim
// and Docker bootstrap names its owner `default`, so an id-only key made two
// backends share one namespace. Before /me resolves, the unscoped legacy
// keys (`nymeria-threads`, etc.) are used. The first resolve of a scope
// carries older-format data into it, once, as a move (the account-only key,
// else the unscoped one).
//
// Keys that should NEVER be namespaced live in NON_SCOPED_KEYS: the config
// store itself (loads before identity exists) and saved-connections (picking
// which backend to talk to is pre-identity).

const NON_SCOPED_KEYS = new Set<string>([
  'nymeria-config',
  'nymeria-saved-connections',
  'nymeria-active-connection-id',
]);

// Keys namespaced per scope and carried forward on its first resolve. Keep
// this list in sync with the `STORAGE_KEY` constants in per-feature stores.
const SCOPED_KEY_BASES = [
  'nymeria-threads',
  'nymeria-current-thread',
  'nymeria-thread-folders',
  'nymeria-thread-team-ui',
  'nymeria-thread-sort-mode',
  'nymeria-ui',
  'nymeria-ui-mobile',
];

let currentScope: IdentityScope | null = null;
let reloadGeneration = 0;

/**
 * Return the localStorage key to use for a given base key, scoped to the
 * current backend and account. Falls back to the base key if identity hasn't
 * been resolved yet (legacy path).
 */
export function scopedKey(base: string): string {
  if (NON_SCOPED_KEYS.has(base)) return base;
  return scopedStorageKey(base, currentScope);
}

/** The backend + account the scoped stores belong to; null before /me resolves. */
export function currentIdentityScope(): IdentityScope | null {
  return currentScope;
}

/**
 * Bumped every time the identity reload hooks fire (a connection switch, an
 * account change, a sign-out). An async flow that captured it before an
 * await and finds it changed afterwards must drop its result: the backend it
 * was talking to is no longer the live one.
 */
export function identityReloadGeneration(): number {
  return reloadGeneration;
}

// ---------------------------------------------------------------------------
// Reload hook registry: the reset contract for backend-scoped client state.
// Every store that caches anything a backend served registers here and drops
// it (or re-reads its scoped localStorage) when the hooks fire: on every
// scope change and on every explicit connection switch (forceReload).
// ---------------------------------------------------------------------------

type ReloadHook = () => void;
const reloadHooks: Set<ReloadHook> = new Set();

export function registerIdentityReloadHook(hook: ReloadHook): () => void {
  reloadHooks.add(hook);
  return () => reloadHooks.delete(hook);
}

function carryForwardScopedKeys(scope: IdentityScope): void {
  if (typeof localStorage === 'undefined') return;
  for (const base of SCOPED_KEY_BASES) {
    carryForwardScopedKey(localStorage, base, scope);
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

  // Seed the module-level scope with whatever identity is persisted so stores
  // that load before refreshIdentity() runs still pick the right keys. The
  // carry-forward runs here too: those stores read the scoped keys at
  // construction, which happens right after this module evaluates.
  if (initial.identity && initial.apiUrl) {
    currentScope = identityScope(initial.apiUrl, initial.identity.id);
    carryForwardScopedKeys(currentScope);
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
    reloadGeneration += 1;
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
    currentScope = null;
    // A sign-out (chosen, or an expired token) lands back on the setup
    // surface; re-arm the stored-URL probe so it opens on sign-in again.
    serverProbe = 'skipped';
    serverProbePromise = null;
    saveCurrentConfig();
    notifyIdentityReloadHooks(label);
  }

  /**
   * Adopt a resolved identity for the backend at `url`. A changed scope
   * (another backend, or another account) carries older-format keys forward
   * and fires the reload hooks; `forceReload` fires them on an unchanged
   * scope too (an explicit connection switch always resets).
   */
  function adoptIdentity(data: AccountIdentity, url: string, forceReload: boolean): void {
    const nextScope = identityScope(url, data.id);
    const scopeChanged = !sameIdentityScope(currentScope, nextScope);
    identity = data;
    currentScope = nextScope;
    if (scopeChanged) carryForwardScopedKeys(nextScope);
    if (scopeChanged || forceReload) notifyIdentityReloadHooks('identity');
    saveCurrentConfig();
  }

  /**
   * /me did not resolve. A blip on the connection the scope already belongs
   * to keeps it (a focus refresh during a network hiccup must not wipe the
   * app). After the backend moved, or on a forced switch, the previous scope
   * must not stay live: drop it and reset, so the new backend starts from
   * empty caches instead of showing (and writing back) the old one's.
   */
  function settleUnresolvedIdentity(url: string, forceReload: boolean): void {
    const backendMoved = currentScope !== null && currentScope.backend !== normalizeBackendUrl(url);
    if (!forceReload && !backendMoved) return;
    identity = null;
    currentScope = null;
    saveCurrentConfig();
    notifyIdentityReloadHooks('unresolved identity');
  }

  /**
   * Fetch GET /me using the current apiUrl + apiKey, update identity state,
   * and fire the reload hooks when the scope (backend + account) changed or
   * `forceReload` is set, so per-feature stores drop the previous backend's
   * state and re-read their scoped keys.
   *
   * Returns the new identity on success, or null if the request failed
   * (missing credentials, network error, 401, etc.); callers should treat
   * null as "route back to SetupWizard". A result for a connection that
   * changed while /me was in flight is dropped (the newer call owns the
   * scope).
   */
  async function refreshIdentity(
    options: { forceReload?: boolean } = {}
  ): Promise<AccountIdentity | null> {
    const forceReload = options.forceReload === true;
    const url = apiUrl;
    const key = apiKey;
    const connectionMoved = () => apiUrl !== url || apiKey !== key;
    if (!url || !key) {
      settleUnresolvedIdentity(url, forceReload);
      return null;
    }
    try {
      const base = url.replace(/\/$/, '');
      const response = await fetch(`${base}/me`, {
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${key}`,
        },
      });
      if (connectionMoved()) return null;
      if (!response.ok) {
        if (response.status === 401) {
          // Token no longer valid. Clear the auth session immediately so the
          // root route renders SetupWizard instead of mounting app panels that
          // will all fail with 401s.
          clearAuthSession('refreshIdentity auth failure');
        } else {
          settleUnresolvedIdentity(url, forceReload);
        }
        return null;
      }
      const data = (await response.json()) as AccountIdentity;
      if (connectionMoved()) return null;
      adoptIdentity(data, url, forceReload);
      return data;
    } catch (e) {
      console.error('refreshIdentity failed:', e);
      if (!connectionMoved()) settleUnresolvedIdentity(url, forceReload);
      return null;
    }
  }

  function clearIdentity() {
    identity = null;
    currentScope = null;
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
    const url = apiUrl;
    const key = apiKey;
    const base = url.replace(/\/$/, '');
    const response = await fetch(`${base}/me`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${key}`,
      },
      body: JSON.stringify({ display_name: trimmed }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `Failed to update display name (${response.status})`);
    }
    const data = (await response.json()) as AccountIdentity;
    // A switch that landed while the PATCH was in flight owns the identity now.
    if (apiUrl === url && apiKey === key) adoptIdentity(data, url, false);
    return data;
  }

  // Served-by-backend detection. Probed at most once per page load; the
  // outcome drives first-open routing (utils/firstRun.ts): a page served by a
  // Nymeria backend opens on the token-only sign-in instead of the install
  // hub. 'skipped' under Tauri or without a window, where the probe is
  // meaningless and the full hub is the only path.
  let originProbe = $state<OriginProbe>('skipped');
  let originProbePromise: Promise<string | null> | null = null;
  // The desktop app's counterpart (#323): the origin probe never runs under
  // Tauri, so a first open there asks the STORED backend URL whether it is
  // already set up (GET /health `configured`). 'served' means yes and the
  // setup surface opens on the token-only sign-in; web builds skip it.
  let serverProbe = $state<OriginProbe>('skipped');
  let serverProbePromise: Promise<string | null> | null = null;

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

  // `retry` re-asks after a settled answer (the managed local backend may
  // not have been listening at the first ask; the root route retries when
  // it becomes ready). Reachable-but-not-configured, unreachable, and
  // too-old-to-say all settle as 'unserved' on purpose: the routing
  // decision is the same, the hub, and the connect step reports the rest.
  function probeServerConfigured(options: { retry?: boolean } = {}): Promise<string | null> {
    if (serverProbePromise && !(options.retry && serverProbe !== 'pending')) {
      return serverProbePromise;
    }
    const url = apiUrl.trim();
    const applies = shouldProbeStoredServer({
      hasWindow: typeof window !== 'undefined',
      isTauri: isTauriRuntime(),
      url,
      setupCompleted,
    });
    if (!applies) {
      serverProbe = 'skipped';
      serverProbePromise = null;
      return Promise.resolve(null);
    }
    serverProbePromise = runOriginProbe(
      async () => ((await readServerConfigured(url)) === true ? url : null),
      (state) => {
        serverProbe = state;
      }
    );
    return serverProbePromise;
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
    get serverProbe(): OriginProbe {
      return serverProbe;
    },
    probeServerConfigured,
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
      currentScope = null;
      originProbe = 'skipped';
      originProbePromise = null;
      serverProbe = 'skipped';
      serverProbePromise = null;
      applyTheme('light');
      saveCurrentConfig();
    }
  };
}

export const configStore = createConfigStore();
