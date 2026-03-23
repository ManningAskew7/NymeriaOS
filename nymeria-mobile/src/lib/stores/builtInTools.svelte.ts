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

const CATEGORY_INFO: Record<ToolCategory, { name: string; icon: string; description: string }> = {
  core: { name: 'Core', icon: 'terminal', description: 'Essential system tools like bash, file operations, and web search' },
  memory: { name: 'Memory', icon: 'brain', description: 'Tools for saving and retrieving user memories and preferences' },
  self_modify: { name: 'Self-Modify', icon: 'code', description: 'Tools that allow Nymeria to modify her own code (sensitive)' },
  todo: { name: 'TODOs', icon: 'list', description: 'Task management and scheduling tools' },
  trigger: { name: 'Triggers', icon: 'zap', description: 'Event-driven trigger management tools' },
  email: { name: 'Email', icon: 'mail', description: 'Outlook email tools for reading, sending, and managing mail' },
  browser: { name: 'Browser', icon: 'globe', description: 'Playwright browser automation tools for web interaction' },
  calendar: { name: 'Calendar', icon: 'calendar', description: 'Google Calendar tools for managing events and schedules' },
  google_docs: { name: 'Google Docs', icon: 'file-text', description: 'Google Docs tools for creating, reading, and writing documents' },
  custom: { name: 'Custom', icon: 'puzzle', description: 'User-created custom tools (HTTP, MCP, etc.)' },
  mcp_server: { name: 'MCP Servers', icon: 'server', description: 'Tools auto-discovered from MCP servers' }
};

function getToolByName(name: string): BuiltInTool | undefined {
  return tools.find((t) => t.name === name);
}

function getEnabledTools(): BuiltInTool[] {
  return tools.filter((t) => t.enabled);
}

function getDisabledTools(): BuiltInTool[] {
  return tools.filter((t) => !t.enabled);
}

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

async function setToolEnabled(toolName: string, enabled: boolean, userId: string = 'default'): Promise<boolean> {
  loading = true;
  error = null;
  try {
    await api.setToolEnabled(userId, toolName, enabled);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update tool';
    return false;
  } finally {
    loading = false;
  }
}

async function clearToolOverride(toolName: string, userId: string = 'default'): Promise<boolean> {
  loading = true;
  error = null;
  try {
    await api.clearToolOverride(userId, toolName);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to clear override';
    return false;
  } finally {
    loading = false;
  }
}

async function setCategoryEnabled(category: string, enabled: boolean, userId: string = 'default'): Promise<boolean> {
  loading = true;
  error = null;
  try {
    await api.setCategoryEnabled(userId, category, enabled);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update category';
    return false;
  } finally {
    loading = false;
  }
}

async function setToolConfig(toolName: string, config: Record<string, unknown>, userId: string = 'default'): Promise<boolean> {
  loading = true;
  error = null;
  try {
    await api.setToolConfig(userId, toolName, config);
    await loadTools(userId);
    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update config';
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
    return false;
  } finally {
    loading = false;
  }
}

function clearError(): void {
  error = null;
}

export const builtInToolsStore = {
  get tools() { return tools; },
  get byCategory() { return byCategory; },
  get preferences() { return preferences; },
  get loading() { return loading; },
  get loaded() { return loaded; },
  get error() { return error; },

  getCategoryInfo: (category: ToolCategory) => CATEGORY_INFO[category],
  getToolByName,
  getEnabledTools,
  getDisabledTools,

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
