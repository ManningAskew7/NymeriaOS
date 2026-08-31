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

/**
 * Will the backend actually poll and fire this trigger?
 *
 * `enabled` alone stopped answering that when the auto-pause policy landed: a
 * trigger stopped after repeated action failures keeps `enabled: true` and
 * carries an `auto_paused_at` stamp instead. Every count, grouping and label
 * that means "live" goes through here so none of them overstate it.
 */
export function isTriggerRunning(trigger: Trigger): boolean {
  return trigger.enabled && !trigger.auto_paused_at;
}

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

  // Lift a backend auto-pause. The response is the repaired trigger, so the
  // row is replaced from it rather than reloaded: the badge, the failure count
  // and the health status all settle in one paint.
  async function resumeTrigger(id: string): Promise<Trigger> {
    const resumed = await api.resumeTrigger(id);
    triggers = triggers.map(t => t.id === id ? resumed : t);
    return resumed;
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

    get enabledCount() { return triggers.filter(isTriggerRunning).length; },
    get activeTriggers() { return triggers.filter(isTriggerRunning); },
    // Everything that is not running, for either reason: switched off by the
    // user OR auto-paused by the policy. Surfaces that show the two causes
    // apart (the feed's groups) split them themselves.
    get pausedTriggers() { return triggers.filter(t => !isTriggerRunning(t)); },

    threadTriggers,
    loadTriggers,
    loadSources,
    createTrigger,
    updateTrigger,
    resumeTrigger,
    deleteTrigger,
    testTrigger,
    getExecutions,
    startPolling,
    stopPolling,
    clearError,
  };
}

export const triggersStore = createTriggersStore();
