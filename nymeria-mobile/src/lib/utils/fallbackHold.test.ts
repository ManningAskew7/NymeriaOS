import { describe, expect, it } from 'vitest';

import type { ActiveLLMFallback, ThreadConfig } from '$lib/types';
import { fallbackRevertLabel, fallbackRevertTarget } from './fallbackHold';

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
