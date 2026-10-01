/**
 * A component-held copy of something one backend served: the settings
 * panels' GET /settings snapshot, which seeds a form that Save sends back
 * whole (#242 review). It is dropped on every identity reload hook, so any
 * connection switch, from any path, empties it before the api client
 * targets the new backend, and the component's load effect then reloads it
 * from there. A load in flight across the switch lands nothing (neither the
 * value nor the form seeding) and does not block the next load.
 *
 * Shared byte-for-byte by desktop and mobile (drift gate EXACT_MATCH).
 */

import { registerIdentityReloadHook } from './config.svelte';

export interface BackendScopedValue<T> {
  /** The live backend's value; null before a load lands and after a switch. */
  readonly value: T | null;
  readonly loading: boolean;
  /** The last load failed; cleared by the next load or a switch. */
  readonly failed: boolean;
  /** Join the identity reload hooks; returns the leave, for `onMount`. */
  attach(): () => void;
  /**
   * Fetch, keep the value and `apply` it (seed the form), unless a reload
   * hook fired meanwhile: then nothing lands. Resolves true when this load's
   * value is the live one.
   */
  load(fetch: () => Promise<T>, apply?: (value: T) => void): Promise<boolean>;
}

export function createBackendScopedValue<T>(label: string): BackendScopedValue<T> {
  let value = $state.raw<T | null>(null);
  let loading = $state(false);
  let failed = $state(false);
  let generation = 0;

  function drop(): void {
    generation += 1;
    value = null;
    loading = false;
    failed = false;
  }

  return {
    get value() {
      return value;
    },
    get loading() {
      return loading;
    },
    get failed() {
      return failed;
    },
    attach() {
      return registerIdentityReloadHook(drop);
    },
    async load(fetch, apply) {
      const started = generation;
      loading = true;
      failed = false;
      try {
        const next = await fetch();
        if (started !== generation) return false;
        value = next;
        apply?.(next);
        return true;
      } catch (e) {
        if (started !== generation) return false;
        console.error(`Failed to load ${label}:`, e);
        failed = true;
        return false;
      } finally {
        if (started === generation) loading = false;
      }
    },
  };
}
