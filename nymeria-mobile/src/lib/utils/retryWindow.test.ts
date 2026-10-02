import { describe, expect, it } from 'vitest';
import { RETRY_AFTER_MS, insideRetryWindow } from './retryWindow';

// The window the models store (#445) and the provider model pickers (#453)
// wait out after an empty or failed listing.
describe('insideRetryWindow', () => {
  const T0 = 1_700_000_000_000;

  it("is 60 s: the OpenRouter catalog's backend failure-cache TTL, a client-side rate limit for the pickers", () => {
    expect(RETRY_AFTER_MS).toBe(60_000);
  });

  it('holds a retry back until the window has fully passed', () => {
    expect(insideRetryWindow(T0, T0)).toBe(true);
    expect(insideRetryWindow(T0, T0 + RETRY_AFTER_MS - 1)).toBe(true);
    expect(insideRetryWindow(T0, T0 + RETRY_AFTER_MS)).toBe(false);
    expect(insideRetryWindow(T0, T0 + 10 * RETRY_AFTER_MS)).toBe(false);
  });

  it('holds nothing back before any empty or failed answer', () => {
    expect(insideRetryWindow(null, T0)).toBe(false);
  });
});
