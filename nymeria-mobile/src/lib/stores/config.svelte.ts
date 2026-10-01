import type { AccountIdentity, AppConfig, ThemeName } from '$lib/types';
import { applyTheme } from '$lib/themes';
import {
  carryForwardScopedKey,
  identityScope,
  normalizeBackendUrl,
  sameIdentityScope,
  scopedStorageKey,
  type IdentityScope
} from '$lib/utils/identityScope';

const STORAGE_KEY = 'nymeria-config';

// ---------------------------------------------------------------------------
// Identity-scoped localStorage helpers
// ---------------------------------------------------------------------------
// Mirror of nymeria-desktop/src/lib/stores/config.svelte.ts; keep in sync.
// Per-identity data lives under `<base>-<account_id>@<backend_url>` once /me
// has resolved (`utils/identityScope.ts` owns the format, #242): the scope is
// the backend PLUS the account, because every backend names its owner
// `default`. Before /me returns, the unscoped legacy keys are used. The first
// resolve of a scope carries older-format data into it, once, as a move.

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

let currentScope: IdentityScope | null = null;
let reloadGeneration = 0;

export function scopedKey(base: string): string {
  if (NON_SCOPED_KEYS.has(base)) return base;
  return scopedStorageKey(base, currentScope);
}

/** The backend + account the scoped stores belong to; null before /me resolves. */
export function currentIdentityScope(): IdentityScope | null {
  return currentScope;
}

/**
 * Bumped every time the identity reload hooks fire. An async flow that
 * captured it before an await and finds it changed afterwards must drop its
 * result: the backend it was talking to is no longer the live one.
 */
export function identityReloadGeneration(): number {
  return reloadGeneration;
}

// Reload hook registry: the reset contract for backend-scoped client state.
// Hooks fire on every scope change and on every explicit connection switch.
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
      apiUrl: '',
      apiKey: '',
      theme: 'light',
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
      if (!config.theme) config.theme = 'light';
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
    theme: 'light',
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
  let theme = $state<ThemeName>(initial.theme ?? 'light');
  let suppressAttachmentWarnings = $state(initial.suppressAttachmentWarnings ?? false);
  let showAutonomousPrompts = $state(initial.showAutonomousPrompts ?? true);
  let describeToolCalls = $state(initial.describeToolCalls ?? true);
  let identity = $state<AccountIdentity | null>(initial.identity ?? null);

  // Seed the scope from the persisted identity before the other stores
  // construct and read their scoped keys (they import this module).
  if (identity && apiUrl) {
    currentScope = identityScope(apiUrl, identity.id);
    carryForwardScopedKeys(currentScope);
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
    theme = config.theme ?? 'light';
    suppressAttachmentWarnings = config.suppressAttachmentWarnings ?? false;
    showAutonomousPrompts = config.showAutonomousPrompts ?? true;
    describeToolCalls = config.describeToolCalls ?? true;
    identity = config.identity ?? null;
    currentScope = identity && apiUrl ? identityScope(apiUrl, identity.id) : null;
    applyTheme(theme);
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
   * to keeps it; after the backend moved, or on a forced switch, the previous
   * scope must not stay live: drop it and reset, so the new backend starts
   * from empty caches instead of showing (and writing back) the old one's.
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
   * Fetch GET /me with the live config and update identity state. The reload
   * hooks fire when the scope (backend + account) changed, or always with
   * `forceReload` (the connection switch, `backendSwitch.svelte.ts`). A result
   * for a connection that changed while /me was in flight is dropped.
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
   * Mobile restores Capacitor Preferences after stores are constructed. Reload
   * the persisted config into live state, then ask scoped stores to re-read.
   */
  function reloadFromStorage(label: string = 'reloadFromStorage'): void {
    applyLoadedConfig(loadConfig());
    if (currentScope) {
      carryForwardScopedKeys(currentScope);
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
      theme = 'light';
      suppressAttachmentWarnings = false;
      showAutonomousPrompts = true;
      describeToolCalls = true;
      identity = null;
      currentScope = null;
      applyTheme('light');
      saveCurrentConfig();
    },
  };
}

export const configStore = createConfigStore();
