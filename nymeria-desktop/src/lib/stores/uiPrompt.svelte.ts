/**
 * Holds the in-flight ui_prompt (if any). The autonomous SSE handler sets
 * the active prompt on the `ui_prompt` event and clears it on
 * `ui_prompt_result` (any client answered, or the backend published a
 * timeout/abort closure). The UiPromptModal component subscribes here.
 *
 * Only one prompt can be active at a time; if a second `ui_prompt` arrives
 * while one is open, it replaces the first (mirrors authPrompt: the agent
 * blocks on each prompt, so concurrent prompts only happen across threads
 * and the latest one wins).
 */

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
