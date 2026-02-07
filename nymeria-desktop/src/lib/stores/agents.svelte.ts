/**
 * Sub-Agents Store
 *
 * Manages sub-agent definitions for the frontend.
 * Provides reactive state and actions for CRUD operations.
 */

import { api } from '$lib/services/api.svelte';
import type {
  SubAgent,
  SubAgentCreateRequest,
  SubAgentUpdateRequest,
  SubAgentTestResponse
} from '$lib/types';

// State
let agents = $state<SubAgent[]>([]);
let loading = $state(false);
let loaded = $state(false);  // Track if initial load has been attempted
let error = $state<string | null>(null);
let selectedAgentName = $state<string | null>(null);

// Getters
function getAgents(): SubAgent[] {
  return agents;
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

function getSelectedAgentName(): string | null {
  return selectedAgentName;
}

function getSelectedAgent(): SubAgent | undefined {
  return agents.find((a) => a.name === selectedAgentName);
}

function getAgentByName(name: string): SubAgent | undefined {
  return agents.find((a) => a.name === name);
}

function getEnabledAgents(): SubAgent[] {
  return agents.filter((a) => a.enabled);
}

function getDisabledAgents(): SubAgent[] {
  return agents.filter((a) => !a.enabled);
}

// Actions
async function loadAgents(): Promise<void> {
  if (loading || loaded) {
    return;
  }

  loading = true;
  error = null;

  try {
    const response = await api.getSubAgents();
    agents = response.agents;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to load agents';
    console.error('Failed to load sub-agents:', e);
  } finally {
    loading = false;
    loaded = true;
  }
}

function resetLoaded(): void {
  loaded = false;
}

async function createAgent(request: SubAgentCreateRequest): Promise<SubAgent | null> {
  loading = true;
  error = null;

  try {
    const newAgent = await api.createSubAgent(request);
    agents = [...agents, newAgent];
    return newAgent;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to create agent';
    console.error('Failed to create sub-agent:', e);
    return null;
  } finally {
    loading = false;
  }
}

async function updateAgent(
  agentName: string,
  request: SubAgentUpdateRequest
): Promise<SubAgent | null> {
  loading = true;
  error = null;

  try {
    const updatedAgent = await api.updateSubAgent(agentName, request);
    agents = agents.map((a) => (a.name === agentName ? updatedAgent : a));
    return updatedAgent;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to update agent';
    console.error('Failed to update sub-agent:', e);
    return null;
  } finally {
    loading = false;
  }
}

async function deleteAgent(agentName: string): Promise<boolean> {
  loading = true;
  error = null;

  try {
    await api.deleteSubAgent(agentName);
    agents = agents.filter((a) => a.name !== agentName);

    // Clear selection if deleted
    if (selectedAgentName === agentName) {
      selectedAgentName = null;
    }

    return true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to delete agent';
    console.error('Failed to delete sub-agent:', e);
    return false;
  } finally {
    loading = false;
  }
}

async function testAgent(
  agentName: string,
  instruction: string
): Promise<SubAgentTestResponse | null> {
  loading = true;
  error = null;

  try {
    const result = await api.testSubAgent(agentName, instruction);
    return result;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to test agent';
    console.error('Failed to test sub-agent:', e);
    return null;
  } finally {
    loading = false;
  }
}

async function toggleAgentEnabled(agentName: string): Promise<boolean> {
  const agent = getAgentByName(agentName);
  if (!agent) return false;

  const updated = await updateAgent(agentName, { enabled: !agent.enabled });
  return updated !== null;
}

function selectAgent(agentName: string | null): void {
  selectedAgentName = agentName;
}

function clearError(): void {
  error = null;
}

// Export store
export const agentsStore = {
  // Getters
  get agents() {
    return getAgents();
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
  get selectedAgentName() {
    return getSelectedAgentName();
  },
  get selectedAgent() {
    return getSelectedAgent();
  },

  // Helper getters
  getAgentByName,
  getEnabledAgents,
  getDisabledAgents,

  // Actions
  loadAgents,
  resetLoaded,
  createAgent,
  updateAgent,
  deleteAgent,
  testAgent,
  toggleAgentEnabled,
  selectAgent,
  clearError
};
