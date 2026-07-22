import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { errorsStore } from './errors.svelte';
import { registerIdentityReloadHook } from './config.svelte';

function createServerSettingsStore() {
  let provider = $state<string | null>(null);
  let providerRoute = $state<string | null>(null);
  let model = $state<string | null>(null);
  let fastModelResolved = $state<string | null>(null);
  let smartModelResolved = $state<string | null>(null);
  let memoryCharLimit = $state<number | null>(null);
  let loading = $state(false);
  let loaded = $state(false);

  // Reset on account/connection switch so consumers re-fetch under the new
  // identity: these are global server settings, so a same-backend account
  // switch reloads identical values, but a saved-connection switch to a
  // different backend must not keep showing the previous server's model.
  registerIdentityReloadHook(() => {
    loaded = false;
    loading = false;
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

    async load(): Promise<void> {
      if (loading) return;
      loading = true;
      try {
        const settings = await api.getServerSettings();
        provider = settings.llm_provider;
        providerRoute = settings.llm_provider_route;
        model = settings.llm_model;
        fastModelResolved = settings.llm_fast_model_resolved;
        smartModelResolved = settings.llm_smart_model_resolved;
        memoryCharLimit = settings.memory_char_limit;
        loaded = true;
      } catch (e) {
        console.error('Failed to load server settings:', e);
        // No panel owns this load (it backs the model chip and inherited
        // provider defaults), so surface the failure through the toast layer.
        errorsStore.push({
          kind: 'generic',
          message: humanizeErrorText(e, { action: 'load', resource: 'server settings' }),
        });
      } finally {
        loading = false;
      }
    },

    async refresh(): Promise<void> {
      loaded = false;
      loading = false;
      await this.load();
    }
  };
}

export const serverSettingsStore = createServerSettingsStore();
