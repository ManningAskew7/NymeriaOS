import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { errorsStore } from './errors.svelte';
import { configStore, registerIdentityReloadHook } from './config.svelte';

// Exported for the store's tests only: each instance registers an identity
// reload hook for life, so the app uses the single `serverSettingsStore`.
export function createServerSettingsStore() {
  let provider = $state<string | null>(null);
  let providerRoute = $state<string | null>(null);
  let model = $state<string | null>(null);
  let fastModelResolved = $state<string | null>(null);
  let smartModelResolved = $state<string | null>(null);
  let memoryCharLimit = $state<number | null>(null);
  let loading = $state(false);
  let loaded = $state(false);
  // Terminal states a failed load latches into, so the effects that keep
  // this store loaded stop re-running it (#381: a `user`-role account got
  // 87 x GET /settings -> 403 in six minutes, a toast each). A refusal is
  // silent: the endpoint is admin-gated by design, and a non-admin simply
  // has no server settings to show. `error` toasts once; a reconnect
  // (MainPanel), a settings save, or refresh() re-arms it.
  let latched403 = $state(false);
  let error = $state<string | null>(null);

  // A KNOWN non-admin identity never asks: GET /settings is admin-gated
  // (require_admin_user), so the request could only 403. Derived, not
  // latched, so a role promotion on the same account (the identity reload
  // hook fires on a scope change or a connection switch, not a role change)
  // un-settles the store and the load effects ask again.
  function knownNonAdmin(): boolean {
    const role = configStore.identity?.role;
    return !!role && role !== 'admin';
  }

  // Bumped by the reload hook; a load that started before it lands nowhere.
  let identityGeneration = 0;

  // Reset on every connection switch and account change. These are the
  // CONNECTED backend's global settings: a switch to another backend whose
  // owner shares this account id used to keep showing, and seeding the Model
  // tab's fast/smart quick-picks from, the previous server's values (#242).
  // The values are dropped, not just flagged stale, so nothing can read the
  // old backend's model between the switch and the new load.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    provider = null;
    providerRoute = null;
    model = null;
    fastModelResolved = null;
    smartModelResolved = null;
    memoryCharLimit = null;
    loaded = false;
    loading = false;
    latched403 = false;
    error = null;
  });

  return {
    get provider() { return provider; },
    get providerRoute() { return providerRoute; },
    get model() { return model; },
    get fastModelResolved() { return fastModelResolved; },
    get smartModelResolved() { return smartModelResolved; },
    get memoryCharLimit() { return memoryCharLimit; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get forbidden() { return latched403 || knownNonAdmin(); },
    get error() { return error; },
    /** Nothing left for the load effects to do: values, a latched refusal, or a latched failure. */
    get settled() { return loaded || this.forbidden || error !== null; },

    async load(): Promise<void> {
      if (loading || loaded || latched403 || error !== null || knownNonAdmin()) return;
      const requestGeneration = identityGeneration;
      loading = true;
      try {
        const settings = await api.getServerSettings();
        if (requestGeneration !== identityGeneration) return;
        provider = settings.llm_provider;
        providerRoute = settings.llm_provider_route;
        model = settings.llm_model;
        fastModelResolved = settings.llm_fast_model_resolved;
        smartModelResolved = settings.llm_smart_model_resolved;
        memoryCharLimit = settings.memory_char_limit;
        loaded = true;
        latched403 = false;
        error = null;
      } catch (e) {
        if (requestGeneration !== identityGeneration) return;
        if ((e as { status?: number } | null)?.status === 403) {
          // Not an admin (the identity was unknown or stale): expected, silent.
          latched403 = true;
          return;
        }
        console.error('Failed to load server settings:', e);
        const message = humanizeErrorText(e, { action: 'load', resource: 'server settings' });
        error = message;
        // No panel owns this load (it backs the model chip and inherited
        // provider defaults), so surface the failure through the toast layer,
        // once: the latch above stops the effects from re-running it.
        errorsStore.push({ kind: 'generic', message });
      } finally {
        if (requestGeneration === identityGeneration) loading = false;
      }
    },

    async refresh(): Promise<void> {
      loaded = false;
      latched403 = false;
      error = null;
      // `loading` is left alone: an in-flight load lands on its own and
      // load() refuses to race it.
      await this.load();
    }
  };
}

export const serverSettingsStore = createServerSettingsStore();
