/**
 * Custom Tools Store
 *
 * Manages custom tool definitions for the frontend.
 * Provides reactive state and actions for CRUD operations.
 */

import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type {
  CustomTool,
  CustomToolCreateRequest,
  CustomToolUpdateRequest,
  CustomToolTestResponse
} from '$lib/types';

function createToolsStore() {
  // State
  let tools = $state<CustomTool[]>([]);
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state<string | null>(null);
  let selectedToolId = $state<string | null>(null);
  // Bumped by the reload hook: a response from the previous backend lands nowhere.
  let identityGeneration = 0;

  // Custom tools are per backend: drop them on every connection switch so
  // the panel reloads instead of listing (and editing) the previous
  // backend's definitions (#242).
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    tools = [];
    loading = false;
    loaded = false;
    error = null;
    selectedToolId = null;
  });

  function current(generation: number): boolean {
    return generation === identityGeneration;
  }

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

    const generation = identityGeneration;
    loading = true;
    error = null;

    try {
      const response = await api.getCustomTools();
      if (!current(generation)) return;
      tools = response.tools;
    } catch (e) {
      if (!current(generation)) return;
      error = humanizeErrorText(e, { action: 'load', resource: 'your custom tools' });
      console.error('Failed to load custom tools:', e);
    } finally {
      if (current(generation)) {
        loading = false;
        loaded = true;
      }
    }
  }

  function resetLoaded(): void {
    loaded = false;
  }

  async function createTool(request: CustomToolCreateRequest): Promise<CustomTool | null> {
    const generation = identityGeneration;
    loading = true;
    error = null;

    try {
      const newTool = await api.createCustomTool(request);
      if (!current(generation)) return null;
      tools = [...tools, newTool];
      return newTool;
    } catch (e) {
      if (!current(generation)) return null;
      error = humanizeErrorText(e, { action: 'create', resource: 'the tool' });
      console.error('Failed to create custom tool:', e);
      return null;
    } finally {
      if (current(generation)) loading = false;
    }
  }

  async function updateTool(
    toolId: string,
    request: CustomToolUpdateRequest
  ): Promise<CustomTool | null> {
    const generation = identityGeneration;
    loading = true;
    error = null;

    try {
      const updatedTool = await api.updateCustomTool(toolId, request);
      if (!current(generation)) return null;
      tools = tools.map((t) => (t.id === toolId ? updatedTool : t));
      return updatedTool;
    } catch (e) {
      if (!current(generation)) return null;
      error = humanizeErrorText(e, { action: 'update', resource: 'the tool' });
      console.error('Failed to update custom tool:', e);
      return null;
    } finally {
      if (current(generation)) loading = false;
    }
  }

  async function deleteTool(toolId: string): Promise<boolean> {
    const generation = identityGeneration;
    loading = true;
    error = null;

    try {
      await api.deleteCustomTool(toolId);
      if (!current(generation)) return false;
      tools = tools.filter((t) => t.id !== toolId);

      if (selectedToolId === toolId) {
        selectedToolId = null;
      }

      return true;
    } catch (e) {
      if (!current(generation)) return false;
      error = humanizeErrorText(e, { action: 'delete', resource: 'the tool' });
      console.error('Failed to delete custom tool:', e);
      return false;
    } finally {
      if (current(generation)) loading = false;
    }
  }

  async function testTool(
    toolId: string,
    params: Record<string, unknown>
  ): Promise<CustomToolTestResponse | null> {
    const generation = identityGeneration;
    loading = true;
    error = null;

    try {
      const result = await api.testCustomTool(toolId, params);
      return current(generation) ? result : null;
    } catch (e) {
      if (!current(generation)) return null;
      error = humanizeErrorText(e, { action: 'test', resource: 'the tool' });
      console.error('Failed to test custom tool:', e);
      return null;
    } finally {
      if (current(generation)) loading = false;
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
  return {
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
}

export const toolsStore = createToolsStore();
