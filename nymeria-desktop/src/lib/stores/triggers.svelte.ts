/**
 * Triggers Store
 *
 * Reactive state for trigger management (CRUD + enable/disable).
 */

import { api } from '$lib/services/api.svelte';
import type { Trigger, TriggerCreateRequest, TriggerUpdateRequest, TriggerSourceInfo } from '$lib/types';

// State
let triggers = $state<Trigger[]>([]);
let sources = $state<Record<string, TriggerSourceInfo>>({});
let loading = $state(false);
let loaded = $state(false);
let error = $state<string | null>(null);

// Actions
async function loadTriggers(): Promise<void> {
  if (loading) return;
  loading = true;
  error = null;
  try {
    triggers = await api.getTriggers();
    loaded = true;
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to load triggers';
    console.error('Failed to load triggers:', e);
  } finally {
    loading = false;
  }
}

async function loadSources(): Promise<void> {
  try {
    sources = await api.getTriggerSources();
  } catch (e) {
    console.error('Failed to load trigger sources:', e);
  }
}

async function createTrigger(request: TriggerCreateRequest): Promise<Trigger> {
  const created = await api.createTrigger(request);
  triggers = [...triggers, created];
  return created;
}

async function updateTrigger(id: string, request: TriggerUpdateRequest): Promise<Trigger> {
  const updated = await api.updateTrigger(id, request);
  triggers = triggers.map(t => t.id === id ? updated : t);
  return updated;
}

async function deleteTrigger(id: string): Promise<void> {
  await api.deleteTrigger(id);
  triggers = triggers.filter(t => t.id !== id);
}

function clearError(): void {
  error = null;
}

// Export store
export const triggersStore = {
  get triggers() { return triggers; },
  get sources() { return sources; },
  get loading() { return loading; },
  get loaded() { return loaded; },
  get error() { return error; },

  get enabledCount() { return triggers.filter(t => t.enabled).length; },

  loadTriggers,
  loadSources,
  createTrigger,
  updateTrigger,
  deleteTrigger,
  clearError
};
