/**
 * Built-in Tools Store
 *
 * Manages built-in tool preferences for the frontend.
 * Provides reactive state and actions for enabling/disabling tools.
 */

import { api } from '$lib/services/api.svelte';
import type { BuiltInTool, ToolCategory, ToolPreferences } from '$lib/types';

// State
let tools = $state<BuiltInTool[]>([]);
let byCategory = $state<Record<ToolCategory, BuiltInTool[]>>({} as Record<ToolCategory, BuiltInTool[]>);
let preferences = $state<ToolPreferences | null>(null);
let loading = $state(false);
let loaded = $state(false);
let error = $state<string | null>(null);

// Category display names and icons
const CATEGORY_INFO: Record<ToolCategory, { name: string; icon: string; description: string }> = {
  core: {
    name: 'Core',
    icon: 'terminal',
    description: 'Essential system tools like bash, file operations, and web search'
  },
  memory: {
    name: 'Memory',
    icon: 'brain',
    description: 'Tools for saving and retrieving user memories and preferences'
  },
  self_modify: {
    name: 'Self-Modify',
    icon: 'code',
    description: 'Tools that allow Nymeria to modify her own code (sensitive)'
  },
  todo: {
    name: 'TODOs',
    icon: 'list',
    description: 'Task management and scheduling tools'
  },
  subagent: {
    name: 'Sub-Agents',
    icon: 'users',
    description: 'Tools for delegating tasks to specialized sub-agents'
  },
  custom: {
    name: 'Custom',
    icon: 'puzzle',
    description: 'User-created custom tools (HTTP, MCP, etc.)'
  }
};

// Getters
function getTools(): BuiltInTool[] {
  return tools;
}

function getByCategory(): Record<ToolCategory, BuiltInTool[]> {
  return byCategory;
}

function getPreferences(): ToolPreferences | null {
  return preferences;
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

function getCategoryInfo(category: ToolCategory) {
  return CATEGORY_INFO[category];
}

function getToolByName(name: string): BuiltInTool | undefined {
  return tools.find((t) => t.name === name);
}

function getEnabledTools(): BuiltInTool[] {
  return tools.filter((t) => t.enabled);
}

function getDisabledTools(): BuiltInTool[] {
  return tools.filter((t) => !t.enabled);
}

// Actions
async function loadTools(userId: string = 'default'): Promise<void> {
  if (loading) return;

  loading = true;
  error = null;

  try {
    const response = await api.getBuiltInTools(userId);
    tools = response.tools;
    byCategory = response.byCategory;
    loaded = true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to load built-in tools';
    console.error('Failed to load built-in tools:', e);
  } finally {
    loading = false;
  }
}

async function loadPreferences(userId: string = 'default'): Promise<void> {
  try {
    preferences = await api.getToolPreferences(userId);
  } catch (e) {
    console.error('Failed to load tool preferences:', e);
  }
}

function resetLoaded(): void {
  loaded = false;
}

async function setToolEnabled(
  toolName: string,
  enabled: boolean,
  userId: string = 'default'
): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.setToolEnabled(userId, toolName, enabled);
    // Reload to get updated state
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update tool';
    console.error('Failed to set tool enabled:', e);
    return false;
  } finally {
    loading = false;
  }
}

async function clearToolOverride(
  toolName: string,
  userId: string = 'default'
): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.clearToolOverride(userId, toolName);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to clear override';
    console.error('Failed to clear tool override:', e);
    return false;
  } finally {
    loading = false;
  }
}

async function setCategoryEnabled(
  category: string,
  enabled: boolean,
  userId: string = 'default'
): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.setCategoryEnabled(userId, category, enabled);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update category';
    console.error('Failed to set category enabled:', e);
    return false;
  } finally {
    loading = false;
  }
}

async function setToolConfig(
  toolName: string,
  config: Record<string, unknown>,
  userId: string = 'default'
): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.setToolConfig(userId, toolName, config);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update config';
    console.error('Failed to set tool config:', e);
    return false;
  } finally {
    loading = false;
  }
}

async function resetToDefaults(userId: string = 'default'): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.resetToolPreferences(userId);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to reset';
    console.error('Failed to reset tool preferences:', e);
    return false;
  } finally {
    loading = false;
  }
}

function clearError(): void {
  error = null;
}

// Export store
export const builtInToolsStore = {
  // Getters
  get tools() {
    return getTools();
  },
  get byCategory() {
    return getByCategory();
  },
  get preferences() {
    return getPreferences();
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

  // Helper getters
  getCategoryInfo,
  getToolByName,
  getEnabledTools,
  getDisabledTools,

  // Actions
  loadTools,
  loadPreferences,
  resetLoaded,
  setToolEnabled,
  clearToolOverride,
  setCategoryEnabled,
  setToolConfig,
  resetToDefaults,
  clearError
};
