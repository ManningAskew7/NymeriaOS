import type { ThreadConfig } from '$lib/types';

/**
 * The model a fallback-hold Revert returns the thread to: its SAVED
 * override model, else the global default (a blank override inherits,
 * the same rule the backend resolves with and names in its end note).
 *
 * Never the hold's `sourceModel`. That is the model that failed when the
 * hold started: after a hold-on-hold it names the intermediate fallback,
 * and after a model change the hold kept (the agent's, a workflow's) it
 * names the model the thread already moved away from (#236).
 *
 * Null when neither is known (a non-admin cannot read the global default).
 */
export function fallbackRevertTarget(
  config: Pick<ThreadConfig, 'llmConfig'> | null | undefined,
  globalModel: string | null | undefined
): string | null {
  const own = (config?.llmConfig?.model ?? '').trim();
  if (own) return own;
  return (globalModel ?? '').trim() || null;
}

/** The Revert button's label: the result of the action, by name when known. */
export function fallbackRevertLabel(
  config: Pick<ThreadConfig, 'llmConfig'> | null | undefined,
  globalModel: string | null | undefined
): string {
  const target = fallbackRevertTarget(config, globalModel);
  return target ? `Revert to ${target}` : 'Revert to the configured model';
}
