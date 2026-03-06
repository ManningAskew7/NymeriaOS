/**
 * Custom Tools Store
 *
 * Manages custom tool definitions for the frontend.
 * Provides reactive state and actions for CRUD operations.
 */

import { api } from '$lib/services/api.svelte';
import type {
  CustomTool,
  CustomToolCreateRequest,
  CustomToolUpdateRequest,
  CustomToolTestResponse
} from '$lib/types';

// State
let tools = $state<CustomTool[]>([]);
let loading = $state(false);
let loaded = $state(false);
let error = $state<string | null>(null);
let selectedToolId = $state<string | null>(null);

// Getters
function getTools(): CustomTool[] {
  return tools;
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

function getSelectedToolId(): string | null {
  return selectedToolId;
}

function getSelectedTool(): CustomTool | undefined {
  return tools.find((t) => t.id === selectedToolId);
}

function getToolById(id: string): CustomTool | undefined {
  return tools.find((t) => t.id === id);
}

function getEnabledTools(): CustomTool[] {
  return tools.filter((t) => t.enabled);
}

function getDisabledTools(): CustomTool[] {
  return tools.filter((t) => !t.enabled);
}

function getToolsByType(type: 'http' | 'mcp'): CustomTool[] {
  return tools.filter((t) => t.implementationType === type);
}

function getToolsByTag(tag: string): CustomTool[] {
  return tools.filter((t) => t.tags.includes(tag));
}

function getAllTags(): string[] {
  const tagSet = new Set<string>();
  tools.forEach((t) => t.tags.forEach((tag) => tagSet.add(tag)));
  return Array.from(tagSet).sort();
}

// Actions
async function loadTools(): Promise<void> {
  if (loading || loaded) {
    return;
  }

  loading = true;
  error = null;

  try {
    const response = await api.getCustomTools();
    tools = response.tools;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to load tools';
    console.error('Failed to load custom tools:', e);
  } finally {
    loading = false;
    loaded = true;
  }
}

function resetLoaded(): void {
  loaded = false;
}

async function createTool(request: CustomToolCreateRequest): Promise<CustomTool | null> {
  loading = true;
  error = null;

  try {
    const newTool = await api.createCustomTool(request);
    tools = [...tools, newTool];
    return newTool;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to create tool';
    console.error('Failed to create custom tool:', e);
    return null;
  } finally {
    loading = false;
  }
}

async function updateTool(
  toolId: string,
  request: CustomToolUpdateRequest
): Promise<CustomTool | null> {
  loading = true;
  error = null;

  try {
    const updatedTool = await api.updateCustomTool(toolId, request);
    tools = tools.map((t) => (t.id === toolId ? updatedTool : t));
    return updatedTool;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update tool';
    console.error('Failed to update custom tool:', e);
    return null;
  } finally {
    loading = false;
  }
}

async function deleteTool(toolId: string): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.deleteCustomTool(toolId);
    tools = tools.filter((t) => t.id !== toolId);

    if (selectedToolId === toolId) {
      selectedToolId = null;
    }

    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to delete tool';
    console.error('Failed to delete custom tool:', e);
    return false;
  } finally {
    loading = false;
  }
}

async function testTool(
  toolId: string,
  params: Record<string, unknown>
): Promise<CustomToolTestResponse | null> {
  loading = true;
  error = null;

  try {
    const result = await api.testCustomTool(toolId, params);
    return result;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to test tool';
    console.error('Failed to test custom tool:', e);
    return null;
  } finally {
    loading = false;
  }
}

async function toggleToolEnabled(toolId: string): Promise<boolean> {
  const tool = getToolById(toolId);
  if (!tool) return false;

  const updated = await updateTool(toolId, { enabled: !tool.enabled });
  return updated !== null;
}

function selectTool(toolId: string | null): void {
  selectedToolId = toolId;
}

function clearError(): void {
  error = null;
}

// Export store
export const toolsStore = {
  get tools() { return getTools(); },
  get loading() { return isLoading(); },
  get loaded() { return isLoaded(); },
  get error() { return getError(); },
  get selectedToolId() { return getSelectedToolId(); },
  get selectedTool() { return getSelectedTool(); },

  getToolById,
  getEnabledTools,
  getDisabledTools,
  getToolsByType,
  getToolsByTag,
  getAllTags,

  loadTools,
  resetLoaded,
  createTool,
  updateTool,
  deleteTool,
  testTool,
  toggleToolEnabled,
  selectTool,
  clearError
};
