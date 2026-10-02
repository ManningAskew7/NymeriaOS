import { describe, expect, it } from 'vitest';

import type { ActiveLLMFallback, ThreadConfig } from '$lib/types';
import {
  fallbackHoldChip,
  fallbackHoldExpiresIn,
  fallbackRevertLabel,
  fallbackRevertTarget,
} from './fallbackHold';

// A hold whose source is NOT what the thread returns to: after a
// hold-on-hold the source is the intermediate fallback that failed.
const HOLD: ActiveLLMFallback = {
  provider: 'anthropic',
  model: 'claude-haiku-4-5',
  sourceProvider: 'openai',
  sourceModel: 'intermediate-fallback',
  holdSeconds: 3600,
  activatedAt: '2026-10-01T00:00:00Z',
  expiresAt: '2026-10-01T01:00:00Z',
};

function savedConfig(llmConfig: ThreadConfig['llmConfig']): ThreadConfig {
  return { threadId: 't-1', llmConfig, activeLlmFallback: HOLD } as ThreadConfig;
}

describe('fallbackRevertTarget', () => {
  it("names the thread's saved override, not the hold's source", () => {
    // The agent's kept model change: config says grok, the hold's source
    // is still the model before it.
    expect(fallbackRevertTarget(savedConfig({ model: 'grok-4.3' }), 'gpt-5.5')).toBe('grok-4.3');
  });

  it('falls back to the global default when the thread inherits', () => {
    expect(fallbackRevertTarget(savedConfig(null), 'gpt-5.5')).toBe('gpt-5.5');
    expect(fallbackRevertTarget(savedConfig({ temperature: 0.2 }), 'gpt-5.5')).toBe('gpt-5.5');
    expect(fallbackRevertTarget(savedConfig({ model: null }), 'gpt-5.5')).toBe('gpt-5.5');
  });

  it('treats a blank or whitespace override as inherit, as the backend does', () => {
    expect(fallbackRevertTarget(savedConfig({ model: '' }), 'gpt-5.5')).toBe('gpt-5.5');
    expect(fallbackRevertTarget(savedConfig({ model: '   ' }), ' gpt-5.5 ')).toBe('gpt-5.5');
  });

  it('is null when neither the override nor the global default is known', () => {
    expect(fallbackRevertTarget(savedConfig(null), '')).toBeNull();
    expect(fallbackRevertTarget(savedConfig(null), null)).toBeNull();
    expect(fallbackRevertTarget(null, undefined)).toBeNull();
  });
});

describe('fallbackRevertLabel', () => {
  it('names the model the revert restores', () => {
    expect(fallbackRevertLabel(savedConfig({ model: 'grok-4.3' }), 'gpt-5.5')).toBe('Revert to grok-4.3');
    expect(fallbackRevertLabel(savedConfig(null), 'gpt-5.5')).toBe('Revert to gpt-5.5');
  });

  it('never names the hold source, even when nothing else is known', () => {
    const label = fallbackRevertLabel(savedConfig(null), '');
    expect(label).toBe('Revert to the configured model');
    expect(label).not.toContain(HOLD.sourceModel);
  });
});

// #441: the header chip (desktop) and badge (mobile) for a live hold. Its
// visibility and copy live here so both apps share them and they are tested
// (neither app renders a component under test, #446).
describe('fallbackHoldChip', () => {
  const BEFORE_EXPIRY = Date.parse('2026-10-01T00:30:00Z');
  const AT_EXPIRY = Date.parse(HOLD.expiresAt as string);

  function withHold(
    hold: Partial<ActiveLLMFallback> | null,
    llmConfig: ThreadConfig['llmConfig'] = { model: 'grok-4.3' }
  ): ThreadConfig {
    return {
      threadId: 't-1',
      llmConfig,
      activeLlmFallback: hold === null ? null : { ...HOLD, ...hold },
    } as ThreadConfig;
  }

  it('is null with no config, no hold, or a null hold', () => {
    expect(fallbackHoldChip(null, 'gpt-5.5', BEFORE_EXPIRY)).toBeNull();
    expect(fallbackHoldChip(undefined, 'gpt-5.5', BEFORE_EXPIRY)).toBeNull();
    expect(fallbackHoldChip({ threadId: 't-1', llmConfig: null } as ThreadConfig, 'gpt-5.5', BEFORE_EXPIRY)).toBeNull();
    expect(fallbackHoldChip(withHold(null), 'gpt-5.5', BEFORE_EXPIRY)).toBeNull();
  });

  it('is null once the hold has expired, even before the backend evicts it', () => {
    // GET /threads/{id}/config returns the raw record and the resolver
    // evicts an expired hold lazily, so a read can still carry a dead one.
    expect(fallbackHoldChip(withHold({}), 'gpt-5.5', AT_EXPIRY)).toBeNull();
    expect(fallbackHoldChip(withHold({}), 'gpt-5.5', AT_EXPIRY + 60_000)).toBeNull();
  });

  it('labels a live hold with the fallback model, provider prefix dropped', () => {
    const chip = fallbackHoldChip(withHold({ model: 'anthropic/claude-haiku-4-5' }), 'gpt-5.5', AT_EXPIRY - 1);
    expect(chip?.label).toBe('fallback: claude-haiku-4-5');
  });

  it('describes the cause, the expiry and what Revert restores, never the hold source', () => {
    const chip = fallbackHoldChip(withHold({}), 'gpt-5.5', BEFORE_EXPIRY);
    const until = new Date(AT_EXPIRY).toLocaleString();
    expect(chip?.description).toBe(
      `Provider-failure fallback active until ${until}. Reverting returns to grok-4.3.`
    );
    expect(chip?.description).not.toContain(HOLD.sourceModel);
  });

  it('names a refusal swap as one', () => {
    const chip = fallbackHoldChip(withHold({ reason: 'refusal' }), 'gpt-5.5', BEFORE_EXPIRY);
    expect(chip?.description.startsWith('Refusal swap active until ')).toBe(true);
  });

  it('a permanent hold reads "until reverted" and never expires', () => {
    const chip = fallbackHoldChip(withHold({ expiresAt: null }), 'gpt-5.5', AT_EXPIRY + 86_400_000);
    expect(chip?.description).toBe(
      'Provider-failure fallback active until reverted. Reverting returns to grok-4.3.'
    );
  });

  it('names the global default when the thread inherits, else the configured model', () => {
    expect(fallbackHoldChip(withHold({}, null), 'gpt-5.5', BEFORE_EXPIRY)?.description)
      .toContain('Reverting returns to gpt-5.5.');
    expect(fallbackHoldChip(withHold({}, null), null, BEFORE_EXPIRY)?.description)
      .toContain('Reverting returns to the configured model.');
  });

  it('keeps showing a hold whose expiry it cannot parse, without inventing a time', () => {
    const chip = fallbackHoldChip(withHold({ expiresAt: 'not a date' }), 'gpt-5.5', BEFORE_EXPIRY);
    expect(chip?.label).toBe('fallback: claude-haiku-4-5');
    expect(chip?.description).toBe('Provider-failure fallback active. Reverting returns to grok-4.3.');
  });
});

describe('fallbackHoldExpiresIn', () => {
  const config = (expiresAt: string | null) =>
    ({ threadId: 't-1', llmConfig: null, activeLlmFallback: { ...HOLD, expiresAt } }) as ThreadConfig;
  const at = Date.parse(HOLD.expiresAt as string);

  it('is the time left on a dated hold, so the chip can drop at expiry', () => {
    expect(fallbackHoldExpiresIn(config(HOLD.expiresAt), at - 90_000)).toBe(90_000);
    expect(fallbackHoldExpiresIn(config(HOLD.expiresAt), at + 5)).toBe(0);
  });

  it('is null when there is nothing to wait for', () => {
    expect(fallbackHoldExpiresIn(null, at)).toBeNull();
    expect(fallbackHoldExpiresIn({ threadId: 't-1', llmConfig: null } as ThreadConfig, at)).toBeNull();
    expect(fallbackHoldExpiresIn(config(null), at)).toBeNull();
    expect(fallbackHoldExpiresIn(config('not a date'), at)).toBeNull();
  });
});
