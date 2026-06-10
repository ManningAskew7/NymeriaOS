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
  let pollInterval: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;
  let identityGeneration = 0;

  // Reset on account switch / sign-out — triggers are per-user.
  // `sources` is a global resource so it's left in place; only the
  // per-user trigger list and load gate are wiped.
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
      // Mark loaded so consumer `$effect` blocks don't loop on a 404/auth error.
      loaded = true;
    } finally {
      if (requestGeneration === identityGeneration) {
        loading = false;
      }
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
