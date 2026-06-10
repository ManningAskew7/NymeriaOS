/**
 * Triggers Store
 *
 * Reactive state for trigger management (CRUD, enable/disable, test, executions).
 */

import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type {
  Trigger,
  TriggerCreateRequest,
  TriggerUpdateRequest,
  TriggerSourceInfo,
  TriggerExecution,
  TriggerTestResult,
} from '$lib/types';

function createTriggersStore() {
  // State
  let triggers = $state<Trigger[]>([]);
  let sources = $state<Record<string, TriggerSourceInfo>>({});
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state<string | null>(null);
  let sourcesError = $state<string | null>(null);
  let pollInterval: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;
  let identityGeneration = 0;

  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    triggers = [];
    loading = false;
    loaded = false;
    error = null;
  });

  function isHidden(): boolean {
    return typeof document !== 'undefined' && document.visibilityState === 'hidden';
  }

  // Actions
  async function loadTriggers(): Promise<void> {
    if (loading) return;
    const requestGeneration = identityGeneration;
    loading = true;
    error = null;
    try {
      const nextTriggers = await api.getTriggers();
      if (requestGeneration !== identityGeneration) return;
      triggers = nextTriggers;
      loaded = true;
    } catch (e) {
      if (requestGeneration !== identityGeneration) return;
      error = humanizeErrorText(e, { action: 'load', resource: 'your triggers' });
      console.error('Failed to load triggers:', e);
      loaded = true;
    } finally {
      if (requestGeneration === identityGeneration) {
        loading = false;
      }
    }
  }

  async function loadSources(): Promise<void> {
    sourcesError = null;
    try {
      sources = await api.getTriggerSources();
    } catch (e) {
      sourcesError = humanizeErrorText(e, { action: 'load', resource: 'trigger sources' });
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

  async function testTrigger(id: string): Promise<TriggerTestResult> {
    return await api.testTrigger(id);
  }

  async function getExecutions(triggerId: string): Promise<TriggerExecution[]> {
    return await api.getTriggerExecutions(triggerId);
  }

  function startPolling(): void {
    if (pollInterval) return;
    pollInterval = setInterval(() => {
      if (!isHidden()) loadTriggers();
    }, 60_000);

    if (typeof document !== 'undefined') {
      visibilityHandler = () => {
        if (!isHidden()) loadTriggers();
      };
      document.addEventListener('visibilitychange', visibilityHandler);
    }
  }

  function stopPolling(): void {
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
    if (visibilityHandler && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', visibilityHandler);
      visibilityHandler = null;
    }
  }

  function clearError(): void {
    error = null;
  }

  function threadTriggers(threadId: string): Trigger[] {
    return triggers.filter(t => t.thread_id === threadId);
  }

  // Export store
  return {
    get triggers() { return triggers; },
    get sources() { return sources; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get error() { return error; },
    get sourcesError() { return sourcesError; },

    get enabledCount() { return triggers.filter(t => t.enabled).length; },
    get activeTriggers() { return triggers.filter(t => t.enabled); },
    get pausedTriggers() { return triggers.filter(t => !t.enabled); },

    threadTriggers,
    loadTriggers,
    loadSources,
    createTrigger,
    updateTrigger,
    deleteTrigger,
    testTrigger,
    getExecutions,
    startPolling,
    stopPolling,
    clearError,
  };
}

export const triggersStore = createTriggersStore();
