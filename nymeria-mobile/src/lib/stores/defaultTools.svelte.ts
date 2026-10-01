import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
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
  // The default list is THIS backend's (loaded, or as last saved), so one
  // entry of it can be edited and the whole list saved back. False after a
  // switch reset the store and after a failed load (which still latches
  // `loaded`, to stop the panel effects looping).
  let listReady = $state(false);
  let identityGeneration = 0;

  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    tools = [];
    defaultToolNames = [];
    callableThreadCount = 0;
    loading = false;
    loaded = false;
    saving = false;
    error = null;
    listReady = false;
  });

  return {
    get tools() { return tools; },
    get defaultToolNames() { return defaultToolNames; },
    get callableThreadCount() { return callableThreadCount; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get saving() { return saving; },
    get error() { return error; },
    get listReady() { return listReady; },

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
        listReady = true;
      } catch (e) {
        if (requestGeneration !== identityGeneration) return;
        error = humanizeErrorText(e, { action: 'load', resource: 'your default tools' });
        listReady = false;
        loaded = true;
      } finally {
        if (requestGeneration === identityGeneration) {
          loading = false;
        }
      }
    },

    resetLoaded() { loaded = false; },

    // A save or reset that a connection switch overtook lands nothing here:
    // its list belongs to the backend it was sent to.
    async save(toolNames: string[], userId?: string): Promise<boolean> {
      const requestGeneration = identityGeneration;
      saving = true;
      error = null;
      try {
        await api.setDefaultTools(toolNames, userId);
        if (requestGeneration !== identityGeneration) return false;
        defaultToolNames = [...toolNames];
        tools = tools.map(t => ({ ...t, is_default: toolNames.includes(t.name) }));
        listReady = true;
        return true;
      } catch (e) {
        if (requestGeneration !== identityGeneration) return false;
        error = humanizeErrorText(e, { action: 'save', resource: 'your default tools' });
        return false;
      } finally {
        if (requestGeneration === identityGeneration) saving = false;
      }
    },

    async reset(userId?: string): Promise<boolean> {
      const requestGeneration = identityGeneration;
      saving = true;
      error = null;
      try {
        await api.resetDefaultTools(userId);
        // Reload to get the full ALL_TOOLS list
        const response = await api.getDefaultTools(userId);
        if (requestGeneration !== identityGeneration) return false;
        tools = response.available_tools;
        defaultToolNames = response.default_tools;
        callableThreadCount = response.callable_thread_count;
        listReady = true;
        return true;
      } catch (e) {
        if (requestGeneration !== identityGeneration) return false;
        error = humanizeErrorText(e, { action: 'reset', resource: 'your default tools' });
        return false;
      } finally {
        if (requestGeneration === identityGeneration) saving = false;
      }
    },

    /**
     * Add or remove one tool from the default set, which is saved whole.
     * Refuses (no request, false) unless the list it builds on is this
     * backend's and no save is in flight: right after a connection switch
     * reset the store, or after a failed load, the list is empty, and one
     * toggle would PUT a one-item list over the backend's whole default set
     * (#242 review).
     */
    async toggleDefaultTool(toolName: string, userId?: string): Promise<boolean> {
      if (!listReady || loading || saving) return false;
      const next = defaultToolNames.includes(toolName)
        ? defaultToolNames.filter((name) => name !== toolName)
        : [...defaultToolNames, toolName];
      return this.save(next, userId);
    },

    clearError() { error = null; },
  };
}

export const defaultToolsStore = createDefaultToolsStore();
