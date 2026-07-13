import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { UiPromptEvent } from '$lib/types';

const mocks = vi.hoisted(() => ({
  submitUiPromptResult: vi.fn(),
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    submitUiPromptResult: mocks.submitUiPromptResult,
  },
}));

import { uiPromptStore } from './uiPrompt.svelte';

function evt(id: string): UiPromptEvent {
  return {
    prompt_id: id,
    thread_id: `thread-${id}`,
    title: '',
    html: '<p>x</p>',
    timeout_seconds: 60,
    expires_at: null,
  };
}

describe('uiPromptStore', () => {
  beforeEach(() => {
    mocks.submitUiPromptResult.mockReset();
    mocks.submitUiPromptResult.mockResolvedValue(undefined);
    // Reset module-level singleton state between tests.
    const active = uiPromptStore.active;
    if (active) uiPromptStore.clearById(active.prompt_id);
  });

  it('open() sets the active prompt', () => {
    uiPromptStore.open(evt('A'));
    expect(uiPromptStore.active?.prompt_id).toBe('A');
  });

  it('a second prompt displaces the first and cancels it so its agent unblocks', () => {
    uiPromptStore.open(evt('A'));
    uiPromptStore.open(evt('B'));
    expect(uiPromptStore.active?.prompt_id).toBe('B');
    expect(mocks.submitUiPromptResult).toHaveBeenCalledTimes(1);
    expect(mocks.submitUiPromptResult).toHaveBeenCalledWith('A', {
      status: 'cancelled',
      values: null,
    });
  });

  it('re-opening the same prompt id does not re-cancel it', () => {
    uiPromptStore.open(evt('A'));
    uiPromptStore.open(evt('A'));
    expect(uiPromptStore.active?.prompt_id).toBe('A');
    expect(mocks.submitUiPromptResult).not.toHaveBeenCalled();
  });

  it('clearById clears only the matching prompt', () => {
    uiPromptStore.open(evt('A'));
    uiPromptStore.clearById('other');
    expect(uiPromptStore.active?.prompt_id).toBe('A');
    uiPromptStore.clearById('A');
    expect(uiPromptStore.active).toBeNull();
  });

  it('resolving a displaced prompt by id never closes the prompt that replaced it', () => {
    // The bug this guards: prompt A resolves while B is showing (B arrived
    // during A's submit round trip). clearById(A) must be a no-op so B stays.
    uiPromptStore.open(evt('A'));
    uiPromptStore.open(evt('B')); // B displaces A; A auto-cancelled
    // Simulate A's in-flight resolve completing now and clearing by A's id.
    uiPromptStore.clearById('A');
    expect(uiPromptStore.active?.prompt_id).toBe('B');
  });
});
