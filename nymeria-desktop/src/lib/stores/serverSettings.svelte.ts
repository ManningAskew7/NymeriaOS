import { api } from '$lib/services/api.svelte';

function createServerSettingsStore() {
  let provider = $state<string | null>(null);
  let model = $state<string | null>(null);
  let loading = $state(false);
  let loaded = $state(false);

  return {
    get provider() { return provider; },
    get model() { return model; },
    get loading() { return loading; },
    get loaded() { return loaded; },

    async load(): Promise<void> {
      if (loading) return;
      loading = true;
      try {
        const settings = await api.getServerSettings();
        provider = settings.llm_provider;
        model = settings.llm_model;
        loaded = true;
      } catch (e) {
        console.error('Failed to load server settings:', e);
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
