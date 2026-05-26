/**
 * One-shot gate for running app initialization the first time the client
 * becomes configured (backend URL + account token both present).
 *
 * The post-config init sequence — backend thread sync, last-thread restore,
 * and the autonomous SSE connect — must fire whether credentials are present
 * at mount (returning user with cached config) or arrive later (a fresh launch
 * that connects through the Setup Wizard). A plain `onMount` runs exactly once,
 * so on the wizard path it executes while still unconfigured, returns early,
 * and never re-runs — leaving the sidebar gated on `initialSyncDone` forever
 * (the "Loading threads…" spinner that never resolves).
 *
 * This gate lets a reactive `$effect` call init exactly once, on the first
 * transition to a configured state, regardless of when that happens.
 */
export function createInitGate() {
  let initialized = false;
  return {
    /**
     * Returns true exactly once: on the first call where `isConfigured` is
     * true. Every later call returns false, so the caller's effect can re-run
     * freely (e.g. when the token is edited) without re-initializing.
     */
    shouldInitialize(isConfigured: boolean): boolean {
      if (initialized || !isConfigured) return false;
      initialized = true;
      return true;
    },
    get initialized() {
      return initialized;
    },
  };
}
