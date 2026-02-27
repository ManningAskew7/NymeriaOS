import { api } from '$lib/services/api.svelte';
import type { DefaultToolInfo } from '$lib/types';

function createDefaultToolsStore() {
  let tools = $state<DefaultToolInfo[]>([]);
  let defaultToolNames = $state<string[]>([]);
  let mode = $state<'legacy' | 'custom'>('legacy');
  let callableThreadCount = $state(0);
  let loading = $state(false);
  let loaded = $state(false);
  let saving = $state(false);
  let error = $state<string | null>(null);

  return {
    get tools() { return tools; },
    get defaultToolNames() { return defaultToolNames; },
    get mode() { return mode; },
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

    async load(userId: string = 'default'): Promise<void> {
      if (loading) return;
      loading = true;
      error = null;
      try {
        const response = await api.getDefaultTools(userId);
        tools = response.available_tools;
        defaultToolNames = response.default_tools;
        mode = response.mode;
        callableThreadCount = response.callable_thread_count;
        loaded = true;
      } catch (e) {
        error = e instanceof Error ? e.message : 'Failed to load default tools';
      } finally {
        loading = false;
      }
    },

    async save(toolNames: string[], userId: string = 'default'): Promise<boolean> {
      saving = true;
      error = null;
      try {
        await api.setDefaultTools(toolNames, userId);
        defaultToolNames = [...toolNames];
        mode = 'custom';
        // Update is_default flags on tool objects
        tools = tools.map(t => ({ ...t, is_default: toolNames.includes(t.name) }));
        return true;
      } catch (e) {
        error = e instanceof Error ? e.message : 'Failed to save default tools';
        return false;
      } finally {
        saving = false;
      }
    },

    async reset(userId: string = 'default'): Promise<boolean> {
      saving = true;
      error = null;
      try {
        await api.resetDefaultTools(userId);
        // Reload to get fresh legacy defaults
        const response = await api.getDefaultTools(userId);
        tools = response.available_tools;
        defaultToolNames = response.default_tools;
        mode = response.mode;
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
