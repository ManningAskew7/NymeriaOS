import type { ThreadConfig, ThreadConfigUpdateRequest } from '$lib/types';
import { api } from '$lib/services/api.svelte';
import { registerIdentityReloadHook } from './config.svelte';

function createThreadConfigStore() {
  let configs = $state<Map<string, ThreadConfig>>(new Map());
  let loading = $state<Set<string>>(new Set());
  // Bumped by the reload hook: a request issued against the previous backend
  // must not land in the new backend's cache.
  let identityGeneration = 0;

  // Keyed by thread id alone, and platform thread ids are deterministic
  // (`telegram_<chat>`, `discord_<guild>_<channel>`), so the same id can name
  // a different thread on another backend. Every connection switch drops the
  // cache, or Thread Settings for such an id would seed from the previous
  // backend's config and Save it onto this one (#242).
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    configs = new Map();
    loading = new Set();
  });

  function stillCurrent(generation: number): boolean {
    return generation === identityGeneration;
  }

  return {
    get configs() {
      return configs;
    },

    getConfig(threadId: string): ThreadConfig | undefined {
      return configs.get(threadId);
    },

    isLoading(threadId: string): boolean {
      return loading.has(threadId);
    },

    isCallableThread(threadId: string): boolean {
      const config = configs.get(threadId);
      return config?.callable ?? false;
    },

    getCallableThreads(): ThreadConfig[] {
      return Array.from(configs.values()).filter((c) => c.callable);
    },

    async loadConfig(threadId: string): Promise<ThreadConfig> {
      const generation = identityGeneration;
      loading = new Set([...loading, threadId]);
      try {
        const config = await api.getThreadConfig(threadId);
        if (stillCurrent(generation)) configs = new Map(configs).set(threadId, config);
        return config;
      } finally {
        if (stillCurrent(generation)) {
          const next = new Set(loading);
          next.delete(threadId);
          loading = next;
        }
      }
    },

    async updateConfig(
      threadId: string,
      updates: ThreadConfigUpdateRequest
    ): Promise<ThreadConfig> {
      const generation = identityGeneration;
      const config = await api.updateThreadConfig(threadId, updates);
      if (stillCurrent(generation)) configs = new Map(configs).set(threadId, config);
      return config;
    },

    async deleteConfig(threadId: string): Promise<void> {
      const generation = identityGeneration;
      await api.deleteThreadConfig(threadId);
      if (!stillCurrent(generation)) return;
      const next = new Map(configs);
      next.delete(threadId);
      configs = next;
    },

    clearCache() {
      configs = new Map();
    },
  };
}

export const threadConfigStore = createThreadConfigStore();
