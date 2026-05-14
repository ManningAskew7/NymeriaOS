/**
 * Unified Tools Store
 *
 * Manages both built-in and custom tools in a single unified interface.
 */

import { api } from '$lib/services/api.svelte';
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

  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    tools = [];
    builtinCount = 0;
    customCount = 0;
    loading = false;
    loaded = false;
    error = null;
  });

  const CATEGORY_INFO: Record<string, { name: string; icon: string; description: string }> = {
    core: { name: 'Core', icon: 'terminal', description: 'Essential system tools' },
    profile: { name: 'Profile', icon: 'brain', description: 'Memory and preferences tools' },
    notepad: { name: 'Notepad', icon: 'sticky-note', description: 'Per-thread persistent notes' },
    self_modify: { name: 'Self-Modify', icon: 'code', description: 'Code modification tools' },
    todo: { name: 'TODOs', icon: 'list', description: 'Task management tools' },
    trigger: { name: 'Triggers', icon: 'zap', description: 'Event-driven trigger tools' },
    email: { name: 'Email', icon: 'mail', description: 'Email tools' },
    browser: { name: 'Browser', icon: 'globe', description: 'Browser automation tools' },
    calendar: { name: 'Calendar', icon: 'calendar', description: 'Calendar tools' },
    google_docs: { name: 'Google Docs', icon: 'file-text', description: 'Google Docs tools' },
    integrations: { name: 'Integrations', icon: 'plug', description: 'Native service integrations' },
    custom: { name: 'Custom', icon: 'puzzle', description: 'User-created custom tools' },
    mcp_server: { name: 'MCP Servers', icon: 'server', description: 'Tools from MCP servers' }
  };

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
      if (!result[tool.category]) result[tool.category] = [];
      result[tool.category].push(tool);
    }
    return result;
  }

  function getCategories(): string[] {
    return Array.from(new Set(tools.map((t) => t.category)));
  }

  function getCategoryInfo(category: string) {
    return CATEGORY_INFO[category] || { name: category, icon: 'tool', description: '' };
  }

  function getToolById(id: string): UnifiedTool | undefined {
    return tools.find((t) => t.id === id);
  }

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

  async function setToolEnabled(toolId: string, enabled: boolean, userId?: string): Promise<boolean> {
    loading = true;
    error = null;
    try {
      await api.setUnifiedToolEnabled(toolId, enabled, userId);
      await loadTools(userId);
      // Sync defaultToolsStore so both stores reflect the change
      defaultToolsStore.resetLoaded();
      return true;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to update tool';
      return false;
    } finally {
      loading = false;
    }
  }

  async function setToolDescription(toolId: string, description: string | null, userId?: string): Promise<boolean> {
    loading = true;
    error = null;
    try {
      await api.setUnifiedToolDescription(toolId, description, userId);
      await loadTools(userId);
      return true;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to update tool description';
      return false;
    } finally {
      loading = false;
    }
  }

  async function setToolConfig(toolId: string, config: Record<string, unknown>, userId?: string): Promise<boolean> {
    loading = true;
    error = null;
    try {
      await api.setUnifiedToolConfig(toolId, config, userId);
      await loadTools(userId);
      return true;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to update tool config';
      return false;
    } finally {
      loading = false;
    }
  }

  function clearError(): void {
    error = null;
  }

  return {
    get tools() { return tools; },
    get builtinTools() { return getBuiltinTools(); },
    get customTools() { return getCustomTools(); },
    get enabledTools() { return getEnabledTools(); },
    get disabledTools() { return getDisabledTools(); },
    get toolsByCategory() { return getToolsByCategory(); },
    get categories() { return getCategories(); },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get error() { return error; },
    get builtinCount() { return builtinCount; },
    get customCount() { return customCount; },

    getCategoryInfo,
    getToolById,

    loadTools,
    resetLoaded,
    setToolEnabled,
    setToolDescription,
    setToolConfig,
    clearError
  };
}

export const unifiedToolsStore = createUnifiedToolsStore();
