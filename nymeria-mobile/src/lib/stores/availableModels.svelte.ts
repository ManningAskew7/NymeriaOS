/**
 * The provider model pickers' live list (GET /models/available) behind the
 * model dropdowns: desktop Settings and Thread Settings' Model tab, mobile
 * Settings and Thread Settings. A panel calls `keepAvailableModelsLoaded`
 * once during component initialization; it owns the load effect and returns
 * the state the dropdown renders. Each answer is also merged into the models
 * store (context hints and effort clamps for any provider).
 *
 * Bounded by construction (#453). The old helper read the state it wrote
 * before its first await, and the api client folds every failure into [],
 * so an empty list re-ran the panel effect on each settle and asked again
 * as fast as the backend answered. Now the effect tracks only the panel's
 * inputs and the connection generation, never the list it writes, and each
 * picker remembers its last ask by `provider|baseUrl` key:
 * - a non-empty answer latches until the key changes, the connection
 *   switches or the panel remounts;
 * - an empty or failed one is retried at most once per retry window
 *   (`utils/retryWindow.ts`, shared with the models store, #445);
 * - a new key asks at once (a provider change, or a return to the previous
 *   provider), and so does a connection switch, which also drops the
 *   previous backend's list at once;
 * - inputs of null mean the panel is closed: nothing is asked, so a panel
 *   that stays mounted (mobile Settings) never asks in the background.
 *
 * Shared byte-for-byte by desktop and mobile (drift gate EXACT_MATCH).
 */

import type { AvailableModel } from '$lib/types';
import { api } from '$lib/services/api.svelte';
import { insideRetryWindow } from '$lib/utils/retryWindow';
import { registerIdentityReloadHook } from './config.svelte';
import { modelsStore } from './models.svelte';

export interface AvailableModelsState {
  models: AvailableModel[];
  loading: boolean;
}

/** What a picker lists: the provider, plus a base URL override when one applies. */
export interface AvailableModelsInputs {
  provider: string;
  baseUrl?: string;
}

/** A picker's last ask. */
interface Ask {
  key: string;
  generation: number;
  /** When the answer landed; null while it is in flight. */
  answeredAt: number | null;
  count: number;
}

// Bumped by every connection switch. Reactive on purpose, and the only
// reactive read in a load: an open picker's effect re-runs on a switch and
// asks the new backend at once.
let generation = $state(0);
registerIdentityReloadHook(() => {
  generation += 1;
});

// Each picker's last ask, by its state. Plain, so no effect tracks it.
const asks = new WeakMap<AvailableModelsState, Ask>();

async function load(state: AvailableModelsState, provider: string, baseUrl: string): Promise<void> {
  const started = generation;
  if (!provider.trim()) {
    asks.delete(state);
    state.models = [];
    state.loading = false;
    return;
  }
  const key = `${provider}|${baseUrl}`;
  const last = asks.get(state);
  if (last && last.key === key && last.generation === started) {
    if (last.answeredAt === null) return; // in flight
    if (last.count > 0) return; // a non-empty list latches
    if (insideRetryWindow(last.answeredAt)) return; // empty or failed: once per window
  }
  // A list from before a switch is the previous backend's: drop it now.
  if (last && last.generation !== started) state.models = [];

  const ask: Ask = { key, generation: started, answeredAt: null, count: 0 };
  asks.set(state, ask);
  state.loading = true;
  let models: AvailableModel[] = [];
  try {
    models = await api.getAvailableModels(provider, baseUrl || undefined);
  } catch {
    // The api client folds failures into [] already; a throw counts the same.
  }
  // Answered by the previous backend: lands nothing, latches nothing.
  if (started !== generation) return;
  ask.answeredAt = Date.now();
  ask.count = models.length;
  modelsStore.mergeAvailableModels(models);
  // A newer ask (another key) owns the list now.
  if (asks.get(state) !== ask) return;
  state.models = models;
  state.loading = false;
}

/**
 * Create a picker's state and the effect that keeps it loaded. Call once
 * during component initialization. `inputs` is read inside the effect:
 * return null while the panel is closed.
 */
export function keepAvailableModelsLoaded(
  inputs: () => AvailableModelsInputs | null
): AvailableModelsState {
  const state = $state<AvailableModelsState>({ models: [], loading: false });
  $effect(() => {
    const next = inputs();
    if (next) void load(state, next.provider, next.baseUrl ?? '');
  });
  return state;
}
