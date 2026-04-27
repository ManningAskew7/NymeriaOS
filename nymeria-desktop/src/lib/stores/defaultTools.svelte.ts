import { api } from '$lib/services/api.svelte';
import { registerIdentityReloadHook } from './config.svelte';
import type { DefaultToolInfo } from '$lib/types';

function createDefaultToolsStore() {
  let tools = $state<DefaultToolInfo[]>([]);
  let defaultToolNames = $state<string[]>([]);
  let callableThreadCount = $state(0);
  let loading = $state(false);
  let loaded = $state(false);
  let saving = $state(false);
  let error = $state<string | null>(null);
  let identityGeneration = 0;

  // Reset on account switch / sign-out so the next consumer re-fetches
  // under the new identity instead of showing stale data.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    tools = [];
    defaultToolNames = [];
    callableThreadCount = 0;
    loading = false;
    loaded = false;
    saving = false;
    error = null;
  });

  return {
    get tools() { return tools; },
    get defaultToolNames() { return defaultToolNames; },
    get callableThreadCount() { return callableThreadCount; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get saving() { return saving; },
    get error() { return error; },

    get totalEnabledCount(): number {
      return defaultToolNames.length + callableThreadCount;
    },

    get toolsByCategory(): Record<string, DefaultToolInfo[]> {
      const result: Record<string, DefaultToolInfo[]> = {};
      for (const tool of tools) {
        if (!result[tool.category]) {
          result[tool.category] = [];
        }
        result[tool.category].push(tool);
      }
      return result;
    },

    async load(userId?: string): Promise<void> {
      if (loading) return;
      const requestGeneration = identityGeneration;
      loading = true;
      error = null;
      try {
        const response = await api.getDefaultTools(userId);
        if (requestGeneration !== identityGeneration) return;
        tools = response.available_tools;
        defaultToolNames = response.default_tools;
        callableThreadCount = response.callable_thread_count;
        loaded = true;
      } catch (e) {
        if (requestGeneration !== identityGeneration) return;
        error = e instanceof Error ? e.message : 'Failed to load default tools';
        // Mark loaded so panel `$effect` doesn't loop on a 404/auth error.
        loaded = true;
      } finally {
        if (requestGeneration === identityGeneration) {
          loading = false;
        }
      }
    },

    resetLoaded() { loaded = false; },

    async save(toolNames: string[], userId?: string): Promise<boolean> {
      saving = true;
      error = null;
      try {
        await api.setDefaultTools(toolNames, userId);
        defaultToolNames = [...toolNames];
        tools = tools.map(t => ({ ...t, is_default: toolNames.includes(t.name) }));
        return true;
      } catch (e) {
        error = e instanceof Error ? e.message : 'Failed to save default tools';
        return false;
      } finally {
        saving = false;
      }
    },

    async reset(userId?: string): Promise<boolean> {
      saving = true;
      error = null;
      try {
        await api.resetDefaultTools(userId);
        // Reload to get the full ALL_TOOLS list
        const response = await api.getDefaultTools(userId);
        tools = response.available_tools;
        defaultToolNames = response.default_tools;
        callableThreadCount = response.callable_thread_count;
        return true;
      } catch (e) {
        error = e instanceof Error ? e.message : 'Failed to reset default tools';
        return false;
      } finally {
        saving = false;
      }
    },

    clearError() { error = null; },
  };
}

export const defaultToolsStore = createDefaultToolsStore();
