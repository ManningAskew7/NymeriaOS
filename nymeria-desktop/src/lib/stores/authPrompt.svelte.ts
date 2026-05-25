/**
 * Holds the in-flight credential prompt (if any). The autonomous SSE handler
 * sets the active prompt on the `auth_prompt` event and clears it on
 * `auth_prompt_resolved` / `auth_prompt_cancelled`. The AuthPromptModal
 * component subscribes here.
 *
 * Only one prompt can be active at a time — if a second `auth_prompt`
 * arrives while one is open, it replaces the first (the agent should not
 * be calling request_credential twice concurrently, so this is mostly a
 * defensive guard).
 */

import type { AuthPromptEvent } from '$lib/types';

interface AuthPromptState {
  active: AuthPromptEvent | null;
}

const state = $state<AuthPromptState>({ active: null });

export const authPromptStore = {
  get active(): AuthPromptEvent | null {
    return state.active;
  },

  open(event: AuthPromptEvent): void {
    state.active = event;
  },

  /** Clear the active prompt iff it matches `promptId` (no-op otherwise).
   * Use this for resolved/cancelled SSE events so we don't accidentally
   * close a newer prompt that replaced an older one. */
  clearById(promptId: string): void {
    if (state.active?.prompt_id === promptId) {
      state.active = null;
    }
  },

  resolveById(promptId: string, result: {
    ok: boolean;
    status: string;
    message?: string | null;
    credentialId?: string | null;
  }): void {
    if (state.active?.prompt_id === promptId) {
      if (state.active.mode !== 'oauth' && state.active.mode !== 'oauth_device') {
        state.active = null;
        return;
      }
      state.active = {
        ...state.active,
        resolution_ok: result.ok,
        resolution_status: result.status,
        resolution_message: result.message ?? null,
        resolved_credential_id: result.credentialId ?? null,
      };
    }
  },

  /** Unconditional clear — used by the modal when the user dismisses. */
  clear(): void {
    state.active = null;
  },
};
