import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type { DefaultToolInfo } from '$lib/types';

// What a whole-list save does to each standard tool's standing (#164), so
// this store, which does not re-read after a save, shows what the backend
// now holds: on the list is "default", dropped from it or declined is
// "declined", anything else keeps its status.
function coreStatusAfterSave(
  tool: DefaultToolInfo,
  toolNames: string[],
  declined: string[]
): DefaultToolInfo['core_status'] {
  if (!tool.core_status) return tool.core_status;
  if (toolNames.includes(tool.name)) return 'default';
  if (tool.core_status === 'default' || declined.includes(tool.name)) return 'declined';
  return tool.core_status;
}

function createDefaultToolsStore() {
  let tools = $state<DefaultToolInfo[]>([]);
  let defaultToolNames = $state<string[]>([]);
  let callableThreadCount = $state(0);
  // #164: standard tools new to this account since it was set up (not in
  // its defaults, never declined), as the backend last reported them.
  let newCoreTools = $state<string[]>([]);
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
    newCoreTools = [];
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
    get newCoreTools() { return newCoreTools; },
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
        newCoreTools = response.new_core_tools ?? [];
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
    // its list belongs to the backend it was sent to. `toolNames` replaces the
    // whole default set, so like toggleDefaultTool it is refused (no request,
    // false) unless this backend's list has loaded: a settings panel's
    // selection seeded before a switch, or from a failed load's empty list,
    // would PUT a subset over the new backend's set (#242 delta review).
    // `declinedCoreTools` (#164) records standard tools absent from the list
    // as declined, so they stop being offered as new.
    async save(toolNames: string[], userId?: string, declinedCoreTools?: string[]): Promise<boolean> {
      if (!listReady) return false;
      const requestGeneration = identityGeneration;
      saving = true;
      error = null;
      const declined = declinedCoreTools ?? [];
      try {
        if (declined.length > 0) {
          await api.setDefaultTools(toolNames, userId, declined);
        } else {
          await api.setDefaultTools(toolNames, userId);
        }
        if (requestGeneration !== identityGeneration) return false;
        defaultToolNames = [...toolNames];
        tools = tools.map(t => ({
          ...t,
          is_default: toolNames.includes(t.name),
          core_status: coreStatusAfterSave(t, toolNames, declined),
        }));
        newCoreTools = newCoreTools.filter(
          (name) => !toolNames.includes(name) && !declined.includes(name)
        );
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
        newCoreTools = response.new_core_tools ?? [];
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

    /**
     * #164: put new standard tools into the SAVED default set. Same guard as
     * toggleDefaultTool: it saves the whole list, so it needs this backend's
     * list loaded. (Desktop's callout "Add"; mirrored for parity.)
     */
    async addNewCoreTools(names: string[], userId?: string): Promise<boolean> {
      if (!listReady || loading || saving || names.length === 0) return false;
      const next = [...defaultToolNames, ...names.filter((name) => !defaultToolNames.includes(name))];
      return this.save(next, userId);
    },

    /**
     * #164: record new standard tools as declined so they are no longer
     * offered as new; the saved list is resent unchanged. Same guard as
     * toggleDefaultTool. (Desktop's callout "Dismiss"; mirrored for parity.)
     */
    async dismissNewCoreTools(names: string[], userId?: string): Promise<boolean> {
      if (!listReady || loading || saving || names.length === 0) return false;
      return this.save([...defaultToolNames], userId, names);
    },

    clearError() { error = null; },
  };
}

export const defaultToolsStore = createDefaultToolsStore();
