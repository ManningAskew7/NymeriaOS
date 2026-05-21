/**
 * Unified Tools Store
 *
 * Manages both built-in and custom tools in a single unified interface.
 * Provides reactive state and actions for tool management.
 */

import { api } from '$lib/services/api.svelte';
import { getCategoryInfo as getToolCategoryInfo } from '$lib/utils/toolCategories';
import { registerIdentityReloadHook } from './config.svelte';
import { defaultToolsStore } from './defaultTools.svelte';
import type { UnifiedTool } from '$lib/types';

function createUnifiedToolsStore() {
  // State
  let tools = $state<UnifiedTool[]>([]);
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state<string | null>(null);
  let builtinCount = $state(0);
  let customCount = $state(0);
  let identityGeneration = 0;

  // Reset everything when the connected user changes — the previous account's
  // tools no longer apply, and consuming panels gate on `loaded` so flipping it
  // back to false makes their `$effect` blocks re-fetch under the new identity.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    tools = [];
    builtinCount = 0;
    customCount = 0;
    loaded = false;
    loading = false;
    error = null;
  });

  // Getters
  function getTools(): UnifiedTool[] {
    return tools;
  }

  function getBuiltinTools(): UnifiedTool[] {
    return tools.filter((t) => t.toolType === 'builtin');
  }

  function getCustomTools(): UnifiedTool[] {
    return tools.filter((t) => t.toolType === 'custom');
  }

  function getEnabledTools(): UnifiedTool[] {
    return tools.filter((t) => t.enabled);
  }

  function getDisabledTools(): UnifiedTool[] {
    return tools.filter((t) => !t.enabled);
  }

  function getToolsByCategory(): Record<string, UnifiedTool[]> {
    const result: Record<string, UnifiedTool[]> = {};
    for (const tool of tools) {
      const cat = tool.category;
      if (!result[cat]) {
        result[cat] = [];
      }
      result[cat].push(tool);
    }
    return result;
  }

  function getCategories(): string[] {
    const cats = new Set(tools.map((t) => t.category));
    return Array.from(cats);
  }

  function isLoading(): boolean {
    return loading;
  }

  function isLoaded(): boolean {
    return loaded;
  }

  function getError(): string | null {
    return error;
  }

  function getCategoryInfo(category: string) {
    return getToolCategoryInfo(category);
  }

  function getToolById(id: string): UnifiedTool | undefined {
    return tools.find((t) => t.id === id);
  }

  // Actions
  async function loadTools(userId?: string): Promise<void> {
    if (loading) return;

    const requestGeneration = identityGeneration;
    loading = true;
    error = null;

    try {
      const response = await api.getUnifiedTools(userId);
      if (requestGeneration !== identityGeneration) return;
      tools = response.tools;
      builtinCount = response.builtinCount;
      customCount = response.customCount;
      loaded = true;
    } catch (e) {
      if (requestGeneration !== identityGeneration) return;
      error = e instanceof Error ? e.message : 'Failed to load tools';
      console.error('Failed to load unified tools:', e);
      // Mark loaded so panel `$effect` doesn't loop on a 404/auth error.
      // resetLoaded() (called on tool changes) or the identity-reload hook
      // will reset this, allowing a fresh attempt.
      loaded = true;
    } finally {
      if (requestGeneration === identityGeneration) {
        loading = false;
      }
    }
  }

  function resetLoaded(): void {
    loaded = false;
  }

  async function setToolEnabled(
    toolId: string,
    enabled: boolean,
    userId?: string
  ): Promise<boolean> {
    loading = true;
    error = null;

    try {
      await api.setUnifiedToolEnabled(toolId, enabled, userId);
      // Drop the mutation loading state before calling loadTools(), which
      // has its own loading guard.
      loading = false;
      await loadTools(userId);
      // Sync defaultToolsStore so banners/settings see the new baseline.
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load(userId);
      return true;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to update tool';
      console.error('Failed to set tool enabled:', e);
      return false;
    } finally {
      loading = false;
    }
  }

  async function setToolDescription(
    toolId: string,
    description: string | null,
    userId?: string
  ): Promise<boolean> {
    loading = true;
    error = null;

    try {
      await api.setUnifiedToolDescription(toolId, description, userId);
      // Reload to get updated state
      await loadTools(userId);
      return true;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to update tool description';
      console.error('Failed to set tool description:', e);
      return false;
    } finally {
      loading = false;
    }
  }

  async function setToolConfig(
    toolId: string,
    config: Record<string, unknown>,
    userId?: string
  ): Promise<boolean> {
    loading = true;
    error = null;

    try {
      await api.setUnifiedToolConfig(toolId, config, userId);
      // Reload to get updated state
      await loadTools(userId);
      return true;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to update tool config';
      console.error('Failed to set tool config:', e);
      return false;
    } finally {
      loading = false;
    }
  }

  function clearError(): void {
    error = null;
  }

  // Export store
  return {
    // Getters
    get tools() {
      return getTools();
    },
    get builtinTools() {
      return getBuiltinTools();
    },
    get customTools() {
      return getCustomTools();
    },
    get enabledTools() {
      return getEnabledTools();
    },
    get disabledTools() {
      return getDisabledTools();
    },
    get toolsByCategory() {
      return getToolsByCategory();
    },
    get categories() {
      return getCategories();
    },
    get loading() {
      return isLoading();
    },
    get loaded() {
      return isLoaded();
    },
    get error() {
      return getError();
    },
    get builtinCount() {
      return builtinCount;
    },
    get customCount() {
      return customCount;
    },

    // Helper getters
    getCategoryInfo,
    getToolById,

    // Actions
    loadTools,
    resetLoaded,
    setToolEnabled,
    setToolDescription,
    setToolConfig,
    clearError
  };
}

export const unifiedToolsStore = createUnifiedToolsStore();
