/**
 * Holds the in-flight browser login handoff (if any). The autonomous SSE
 * handler opens it on `browser_login_started` and marks it ended on
 * `browser_login_ended`; the BrowserLoginModal component subscribes here
 * and feeds the same `endById` from its own frame stream's `login_end`,
 * so whichever signal arrives first wins and the second is a no-op.
 *
 * Only one session can be active at a time, matching the backend's
 * one-session-per-user rule. If a second `browser_login_started` arrives
 * anyway (a stale viewer racing a fresh session), latest wins and the
 * displaced session gets a best-effort `cancelled` end so its tab is not
 * left held behind a viewer nobody can see (mirrors uiPrompt's
 * displace-cancel).
 *
 * `ended` stays IN the store rather than clearing it: an ending that
 * arrives while the viewer is open must show its reason (expired, agent
 * cancelled, thread stopped), not silently vanish the window the user is
 * typing into. The component clears by id once the user closes it.
 */

import { api } from '$lib/services/api.svelte';
import type { BrowserLoginSessionStatus } from '$lib/services/api/browser-login';

export interface ActiveBrowserLogin {
  session: BrowserLoginSessionStatus;
  /** Who opened it: "agent" (chrome_request_login), "command"
   * (/browser login), or "recovered" (found live at app start). */
  origin: string;
  /** Local receipt time. The countdown anchors here plus
   * `seconds_remaining`, never to the server's `expires_at`, so client
   * clock skew can never close a live session early. */
  receivedAtMs: number;
  /** The end reason once the session is over (`completed`, `cancelled`,
   * `expired`, `aborted`, `failed`), null while live. */
  ended: string | null;
}

interface BrowserLoginState {
  active: ActiveBrowserLogin | null;
}

const state = $state<BrowserLoginState>({ active: null });

export const browserLoginStore = {
  get active(): ActiveBrowserLogin | null {
    return state.active;
  },

  open(session: BrowserLoginSessionStatus, origin: string): void {
    const displaced = state.active;
    if (displaced && displaced.session.session_id !== session.session_id) {
      if (!displaced.ended) {
        // Latest wins, but the displaced session must not stay live behind
        // a viewer nobody can see: end it. Best-effort; the backend TTL
        // owns cleanup if this never lands.
        void api
          .endBrowserLoginSession(displaced.session.session_id, 'cancelled')
          .catch(() => {});
      }
    } else if (displaced && displaced.session.session_id === session.session_id) {
      // A re-announce of the session already showing (recovery racing the
      // live event): keep the original receipt anchor.
      return;
    }
    state.active = {
      session,
      origin,
      receivedAtMs: Date.now(),
      ended: null
    };
  },

  /** Mark the active session ended iff the id matches (no-op otherwise).
   * Idempotent: the first reason to land sticks, so the SSE bus event and
   * the viewer's own stream cannot overwrite each other. */
  endById(sessionId: string, reason: string): void {
    const active = state.active;
    if (active && active.session.session_id === sessionId && !active.ended) {
      active.ended = reason || 'cancelled';
    }
  },

  /** Drop the session iff the id matches: the viewer closed. */
  clearById(sessionId: string): void {
    if (state.active?.session.session_id === sessionId) {
      state.active = null;
    }
  }
};
