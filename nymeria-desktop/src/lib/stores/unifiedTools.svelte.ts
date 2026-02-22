/**
 * Unified Tools Store
 *
 * Manages both built-in and custom tools in a single unified interface.
 * Provides reactive state and actions for tool management.
 */

import { api } from '$lib/services/api.svelte';
import type { UnifiedTool, ToolCategory, ToolType } from '$lib/types';

// State
let tools = $state<UnifiedTool[]>([]);
let loading = $state(false);
let loaded = $state(false);
let error = $state<string | null>(null);
let builtinCount = $state(0);
let customCount = $state(0);

// Category display names and icons
const CATEGORY_INFO: Record<string, { name: string; icon: string; description: string }> = {
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
  custom: {
    name: 'Custom',
    icon: 'puzzle',
    description: 'User-created custom tools (HTTP, MCP, etc.)'
  }
};

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
  return (
    CATEGORY_INFO[category] || {
      name: category,
      icon: 'tool',
      description: ''
    }
  );
}

function getToolById(id: string): UnifiedTool | undefined {
  return tools.find((t) => t.id === id);
}

// Actions
async function loadTools(userId: string = 'default'): Promise<void> {
  if (loading) return;

  loading = true;
  error = null;

  try {
    const response = await api.getUnifiedTools(userId);
    tools = response.tools;
    builtinCount = response.builtinCount;
    customCount = response.customCount;
    loaded = true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to load tools';
    console.error('Failed to load unified tools:', e);
  } finally {
    loading = false;
  }
}

function resetLoaded(): void {
  loaded = false;
}

async function setToolEnabled(
  toolId: string,
  enabled: boolean,
  userId: string = 'default'
): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.setUnifiedToolEnabled(userId, toolId, enabled);
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

async function createCustomTool(
  request: {
    id: string;
    name: string;
    description: string;
    parameters?: Record<string, unknown>[];
    http?: Record<string, unknown>;
    mcp?: Record<string, unknown>;
    tags?: string[];
  },
  userId: string = 'default'
): Promise<UnifiedTool | null> {
  loading = true;
  error = null;

  try {
    const tool = await api.createUnifiedTool(request);
    await loadTools(userId);
    return tool;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to create tool';
    console.error('Failed to create custom tool:', e);
    return null;
  } finally {
    loading = false;
  }
}

async function updateCustomTool(
  toolId: string,
  request: {
    name?: string;
    description?: string;
    parameters?: Record<string, unknown>[];
    http?: Record<string, unknown>;
    mcp?: Record<string, unknown>;
    tags?: string[];
  },
  userId: string = 'default'
): Promise<UnifiedTool | null> {
  loading = true;
  error = null;

  try {
    const tool = await api.updateUnifiedTool(toolId, request);
    await loadTools(userId);
    return tool;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update tool';
    console.error('Failed to update custom tool:', e);
    return null;
  } finally {
    loading = false;
  }
}

async function deleteCustomTool(
  toolId: string,
  userId: string = 'default'
): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.deleteUnifiedTool(toolId);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to delete tool';
    console.error('Failed to delete custom tool:', e);
    return false;
  } finally {
    loading = false;
  }
}

async function setToolDescription(
  toolId: string,
  description: string | null,
  userId: string = 'default'
): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.setUnifiedToolDescription(userId, toolId, description);
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
  userId: string = 'default'
): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.setUnifiedToolConfig(userId, toolId, config);
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
export const unifiedToolsStore = {
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
  createCustomTool,
  updateCustomTool,
  deleteCustomTool,
  clearError
};
