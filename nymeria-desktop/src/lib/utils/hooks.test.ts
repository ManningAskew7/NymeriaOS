import { describe, it, expect } from 'vitest';
import {
  describeHookLogic,
  hookActionMeta,
  hookCategory,
  hookEventMeta,
  HOOK_ACTION_META,
} from './hooks';
import type { Hook } from '$lib/types';

/** Minimal hook builder; tests override what they exercise. */
function makeHook(overrides: Partial<Hook>): Hook {
  return {
    id: 'h1',
    name: 'Test hook',
    event: 'prompt_submit',
    action: 'inject_context',
    logic: {},
    text: '',
    matcher: null,
    fire_conditions: [],
    once: false,
    single_use: false,
    enabled: true,
    scope: 'global',
    thread_id: '',
    created_by: 'user',
    created_at: '2026-07-22T00:00:00Z',
    updated_at: '2026-07-22T00:00:00Z',
    system: false,
    ...overrides,
  };
}

describe('hookActionMeta', () => {
  it('resolves the nine authorable actions from the typed table', () => {
    expect(hookActionMeta('inject_context')).toBe(HOOK_ACTION_META.inject_context);
    expect(hookActionMeta('run_workflow').label).toBe('Run workflow');
  });

  it('resolves the system turn_metadata action without crashing', () => {
    const meta = hookActionMeta('turn_metadata');
    expect(meta.label).toBe('Turn metadata');
    expect(meta.icon).toBeTruthy();
    // The crash path was `undefined.hint`; the accessor must always yield one.
    expect(typeof meta.hint).toBe('string');
  });

  it('degrades an unknown wire action to a labeled fallback', () => {
    const meta = hookActionMeta('future_action');
    expect(meta.label).toBe('future_action');
    expect(meta.icon).toBeTruthy();
    expect(meta.hint).toBe('');
  });
});

describe('hookEventMeta', () => {
  it('falls back to the raw name for an unknown event', () => {
    expect(hookEventMeta('future_event').label).toBe('future_event');
  });
});

describe('hookCategory', () => {
  it('files the system turn_metadata action under context', () => {
    expect(hookCategory('turn_metadata')).toBe('context');
  });

  it('defaults unknown actions to reactions', () => {
    expect(hookCategory('future_action')).toBe('reactions');
  });
});

describe('describeHookLogic', () => {
  it('renders the text body for text actions, whitespace-collapsed', () => {
    const hook = makeHook({
      action: 'inject_context',
      logic: { action: 'inject_context', text: 'line one\n  line two' },
    });
    expect(describeHookLogic(hook)).toBe('line one line two');
  });

  it('renders the turn-metadata template itself, newlines preserved', () => {
    const hook = makeHook({
      action: 'turn_metadata' as Hook['action'],
      logic: { action: 'turn_metadata', text: '[Time: {time}]\n[Trigger: {trigger}]' },
    });
    expect(describeHookLogic(hook)).toBe('[Time: {time}]\n[Trigger: {trigger}]');
  });

  it('does not fall through to the raw action name for turn_metadata', () => {
    const hook = makeHook({
      action: 'turn_metadata' as Hook['action'],
      logic: { action: 'turn_metadata' },
    });
    expect(describeHookLogic(hook)).toBe('(built-in template)');
  });

  it('still names truly unknown actions via the fallback', () => {
    const hook = makeHook({
      action: 'future_action' as Hook['action'],
      logic: { action: 'future_action' },
    });
    expect(describeHookLogic(hook)).toBe('future_action');
  });
});
