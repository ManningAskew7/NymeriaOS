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

/**
 * A model id without its provider prefix, every segment of it
 * (`openrouter/anthropic/claude-opus-4.6` reads `claude-opus-4.6`): the
 * short name the header's model and hold chips show.
 */
export function shortModelName(modelId: string): string {
  const parts = modelId.split('/');
  return parts[parts.length - 1];
}

/** The header chip (desktop) or badge (mobile) for a live hold. */
export interface FallbackHoldChip {
  /** `fallback: <model>`, the provider prefix dropped. */
  label: string;
  /**
   * Cause, how long the hold lasts, and what a Revert restores. No
   * wayfinding: where the Revert lives differs per app, so callers append it.
   */
  description: string;
}

type HoldConfig = Pick<ThreadConfig, 'llmConfig' | 'activeLlmFallback'>;

/** Epoch ms the hold lapses at; null for a permanent or undatable hold. */
function holdExpiry(config: Pick<ThreadConfig, 'activeLlmFallback'> | null | undefined): number | null {
  const raw = config?.activeLlmFallback?.expiresAt;
  if (!raw) return null;
  const at = Date.parse(raw);
  return Number.isNaN(at) ? null : at;
}

/**
 * The chip for the thread's fallback hold, or null when there is none to
 * show: no hold, or one whose `expiresAt` has passed. GET
 * /threads/{id}/config returns the raw record and the backend evicts an
 * expired hold lazily (at the next resolve, idle only), so a fresh read can
 * still carry a dead hold; the chip must not claim it (#441).
 *
 * The description names what a Revert RESTORES (`fallbackRevertTarget`),
 * never the hold's `sourceModel` (#236). A hold whose expiry cannot be
 * parsed stays visible, without a time.
 */
export function fallbackHoldChip(
  config: HoldConfig | null | undefined,
  globalModel: string | null | undefined,
  now: number = Date.now()
): FallbackHoldChip | null {
  const hold = config?.activeLlmFallback;
  if (!hold) return null;
  const expiresAt = holdExpiry(config);
  if (expiresAt !== null && expiresAt <= now) return null;

  const cause = hold.reason === 'refusal' ? 'Refusal swap active' : 'Provider-failure fallback active';
  const until = !hold.expiresAt
    ? ' until reverted'
    : expiresAt === null
      ? ''
      : ` until ${new Date(expiresAt).toLocaleString()}`;
  const target = fallbackRevertTarget(config, globalModel);
  return {
    label: `fallback: ${shortModelName(hold.model)}`,
    description: `${cause}${until}. Reverting returns to ${target ?? 'the configured model'}.`,
  };
}

/**
 * Milliseconds until a dated hold lapses (0 once it has), so a header can
 * re-derive its chip at expiry; null when there is nothing to wait for (no
 * hold, a permanent one, or an undatable one).
 */
export function fallbackHoldExpiresIn(
  config: Pick<ThreadConfig, 'activeLlmFallback'> | null | undefined,
  now: number = Date.now()
): number | null {
  const expiresAt = holdExpiry(config);
  return expiresAt === null ? null : Math.max(0, expiresAt - now);
}
