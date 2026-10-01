/**
 * Holds the in-flight ui_prompt (if any). The autonomous SSE handler sets
 * the active prompt on the `ui_prompt` event and clears it on
 * `ui_prompt_result` (any client answered, or the backend published a
 * timeout/abort closure). The UiPromptModal component subscribes here.
 *
 * Only one prompt can be active at a time; if a second `ui_prompt` arrives
 * while one is open, it replaces the first (mirrors authPrompt: the agent
 * blocks on each prompt, so concurrent prompts only happen across threads
 * and the latest one wins). The displaced prompt is resolved as `cancelled`
 * (best-effort POST) so its agent unblocks immediately instead of waiting
 * out its full timeout on a form nobody can see any more.
 */

import { api } from '$lib/services/api.svelte';
import type { UiPromptEvent } from '$lib/types';
import { registerIdentityReloadHook } from './config.svelte';

interface UiPromptState {
  active: UiPromptEvent | null;
}

const state = $state<UiPromptState>({ active: null });

// A prompt belongs to the backend whose agent opened it. On any identity
// change the modal closes: submitting it would post the previous backend's
// prompt id to the new one (#242). A connection switch cancels it on the old
// backend first (`cancelActive`, while the config still names that backend);
// the hook itself only closes, since by then the api client may already
// target another backend.
registerIdentityReloadHook(() => {
  state.active = null;
});

// Resolve a prompt as `cancelled` on the backend the api client names now
// (the url and token are read when the call is made). Best-effort; on
// failure the backend timeout owns cleanup, and a prompt another client
// already answered makes it a harmless delivered=false no-op.
function cancelOnBackend(prompt: UiPromptEvent): void {
  void api
    .submitUiPromptResult(prompt.prompt_id, { status: 'cancelled', values: null })
    .catch(() => {});
}

export const uiPromptStore = {
  get active(): UiPromptEvent | null {
    return state.active;
  },

  open(event: UiPromptEvent): void {
    const displaced = state.active;
    if (displaced && displaced.prompt_id !== event.prompt_id) {
      // Latest wins, but the displaced prompt's agent must not sit blocked
      // until its timeout: resolve it as cancelled.
      cancelOnBackend(displaced);
    }
    state.active = event;
  },

  /**
   * A connection switch is about to repoint the api client: resolve the open
   * prompt as cancelled on the backend whose agent opened it, so that agent
   * wakes now instead of at its timeout, and close the modal. Called by
   * `applyConnection` before the repoint.
   */
  cancelActive(): void {
    const active = state.active;
    if (!active) return;
    state.active = null;
    cancelOnBackend(active);
  },

  /** Clear the active prompt iff it matches `promptId` (no-op otherwise).
   * The one clear path: the `ui_prompt_result` SSE handler and the modal's
   * own resolve both call it by id, so a stale result (or a resolve of a
   * prompt that was already displaced) can never close a newer prompt that
   * replaced an older one. */
  clearById(promptId: string): void {
    if (state.active?.prompt_id === promptId) {
      state.active = null;
    }
  },
};
