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

interface UiPromptState {
  active: UiPromptEvent | null;
}

const state = $state<UiPromptState>({ active: null });

export const uiPromptStore = {
  get active(): UiPromptEvent | null {
    return state.active;
  },

  open(event: UiPromptEvent): void {
    const displaced = state.active;
    if (displaced && displaced.prompt_id !== event.prompt_id) {
      // Latest wins, but the displaced prompt's agent must not sit blocked
      // until its timeout: resolve it as cancelled. Best-effort; on failure
      // the backend timeout still owns cleanup. If another client answered
      // it first, the POST is a harmless delivered=false no-op.
      void api
        .submitUiPromptResult(displaced.prompt_id, { status: 'cancelled', values: null })
        .catch(() => {});
    }
    state.active = event;
  },

  /** Clear the active prompt iff it matches `promptId` (no-op otherwise).
   * Used for `ui_prompt_result` SSE events so a stale result can't close a
   * newer prompt that replaced an older one. */
  clearById(promptId: string): void {
    if (state.active?.prompt_id === promptId) {
      state.active = null;
    }
  },

  /** Unconditional clear, used by the modal after it resolves the prompt. */
  clear(): void {
    state.active = null;
  },
};
