/**
 * Hooks Store
 *
 * Reactive state for lifecycle-hook management (CRUD, enable/disable, test).
 * Unlike triggers, hooks have no runtime/execution state, so there is no
 * source catalog, no execution history, and no polling.
 */

import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type {
  Hook,
  HookCreateRequest,
  HookUpdateRequest,
  HookTestResult,
} from '$lib/types';

function createHooksStore() {
  // State
  let hooks = $state<Hook[]>([]);
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state<string | null>(null);
  let identityGeneration = 0;

  // Reset on account switch / sign-out — hooks are per-user.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    hooks = [];
    loading = false;
    loaded = false;
    error = null;
  });

  // Actions
  async function loadHooks(): Promise<void> {
    if (loading) return;
    const requestGeneration = identityGeneration;
    loading = true;
    error = null;
    try {
      const nextHooks = await api.getHooks();
      if (requestGeneration !== identityGeneration) return;
      hooks = nextHooks;
      loaded = true;
    } catch (e) {
      if (requestGeneration !== identityGeneration) return;
      error = humanizeErrorText(e, { action: 'load', resource: 'your hooks' });
      console.error('Failed to load hooks:', e);
      // Mark loaded so consumer `$effect` blocks don't loop on a 404/auth error.
      loaded = true;
    } finally {
      if (requestGeneration === identityGeneration) {
        loading = false;
      }
    }
  }

  async function createHook(request: HookCreateRequest): Promise<Hook> {
    const created = await api.createHook(request);
    hooks = [...hooks, created];
    return created;
  }

  async function updateHook(id: string, request: HookUpdateRequest): Promise<Hook> {
    const updated = await api.updateHook(id, request);
    hooks = hooks.map(h => h.id === id ? updated : h);
    return updated;
  }

  async function deleteHook(id: string): Promise<void> {
    await api.deleteHook(id);
    hooks = hooks.filter(h => h.id !== id);
  }

  async function testHook(id: string): Promise<HookTestResult> {
    return await api.testHook(id);
  }

  function clearError(): void {
    error = null;
  }

  /**
   * Hooks that apply to a thread: its own thread-scoped hooks plus every
   * global hook (globals fire on all threads). Mirrors the backend list
   * filter (`scope == "global" or thread_id == thread_id`).
   */
  function threadHooks(threadId: string): Hook[] {
    return hooks.filter(h => h.scope === 'global' || h.thread_id === threadId);
  }

  // Export store
  return {
    get hooks() { return hooks; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get error() { return error; },

    get enabledCount() { return hooks.filter(h => h.enabled).length; },
    get activeHooks() { return hooks.filter(h => h.enabled); },
    get pausedHooks() { return hooks.filter(h => !h.enabled); },

    threadHooks,
    loadHooks,
    createHook,
    updateHook,
    deleteHook,
    testHook,
    clearError,
  };
}

export const hooksStore = createHooksStore();
