/**
 * The retry window after a listing came back empty or failed: the OpenRouter
 * catalog behind the models store (#445) and each provider model picker's
 * GET /models/available list (#453). For the catalog it matches the
 * backend, which answers an empty catalog out of its own 60 s failure cache,
 * so an earlier retry can only get the same answer. GET /models/available
 * caches nothing (each call is a live provider listing), so for the pickers
 * it is a client-side rate limit, and a save that can change the answer
 * re-asks at once instead (`refreshAvailableModels`). Without a window, an
 * effect that re-ran on the load's own settle asked again as fast as the
 * backend answered.
 *
 * Shared byte-for-byte by desktop and mobile (drift gate EXACT_MATCH).
 */

export const RETRY_AFTER_MS = 60_000;

/** Whether a listing that came back empty or failed at `emptyAt` still holds back a retry. */
export function insideRetryWindow(emptyAt: number | null, now: number = Date.now()): boolean {
  return emptyAt !== null && now - emptyAt < RETRY_AFTER_MS;
}
