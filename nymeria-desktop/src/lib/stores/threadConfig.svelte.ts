import type { ThreadConfig, ThreadConfigUpdateRequest } from '$lib/types';
import { api } from '$lib/services/api.svelte';

function createThreadConfigStore() {
  let configs = $state<Map<string, ThreadConfig>>(new Map());
  let loading = $state<Set<string>>(new Set());

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

    async loadConfig(threadId: string): Promise<ThreadConfig> {
      loading = new Set([...loading, threadId]);
      try {
        const config = await api.getThreadConfig(threadId);
        configs = new Map(configs).set(threadId, config);
        return config;
      } finally {
        const next = new Set(loading);
        next.delete(threadId);
        loading = next;
      }
    },

    async updateConfig(
      threadId: string,
      updates: ThreadConfigUpdateRequest
    ): Promise<ThreadConfig> {
      const config = await api.updateThreadConfig(threadId, updates);
      configs = new Map(configs).set(threadId, config);
      return config;
    },

    async deleteConfig(threadId: string): Promise<void> {
      await api.deleteThreadConfig(threadId);
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
